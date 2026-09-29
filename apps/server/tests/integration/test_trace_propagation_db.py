"""Trace context across the event bus, against real Postgres — ADR-0018 §2.

The claim under test is narrow and load-bearing: **an asynchronous handler runs inside the trace
of the HTTP request that caused it**. Nothing is faked between the two — a real `OutboxWriter`
signs and inserts a row inside an active span, the real `EventDispatcher` claims it in strict
signature mode, and the span the handler runs in is inspected through a real in-memory exporter.

Four things this proves that a unit test cannot:

* the `traceparent` survives a round trip through a `text` column and `EventEnvelope.from_row`;
* the signature still verifies with a **populated** `trace_id`, which ADR-0007 covers — a trace that
  broke event authentication would be a trade nobody agreed to;
* the chain continues past one hop: an event published *by* a handler carries the same trace;
* an event with no usable `trace_id` gets a root span rather than being parented onto whatever the
  dispatcher happened to be inside.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.config import settings
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.dispatcher import VERIFY_STRICT, EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import get_inbox_table
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from sentinelai.platform.tracing import current_trace_id
from tests.fixtures.kms import kms_for_tests
from tests.fixtures.tracing import in_memory_tracing

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMA = "ingestion"
_EVENT_TYPE = "evidence.ingested"
_CHAINED_TYPE = "evidence.scanned"


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
    name = f"sentinelai_trace_{uuid.uuid4().hex[:8]}"
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
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
        await conn.run_sync(get_outbox_table(_SCHEMA).create, checkfirst=True)
        await conn.run_sync(get_inbox_table(_SCHEMA).create, checkfirst=True)


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
def exporter() -> Iterator[InMemorySpanExporter]:
    """A real provider whose finished spans land in memory, restored afterwards.

    Shared, because restoring the provider correctly is subtler than it looks and getting it wrong
    breaks unrelated tests in a later file — see `tests/fixtures/tracing.py`.
    """
    with in_memory_tracing() as memory:
        yield memory


# --- the production wiring --------------------------------------------------
class _Recorder:
    """A handler that records the trace it ran in, and optionally publishes or fails."""

    def __init__(self, *, chain: bool = False, fail: bool = False) -> None:
        self.trace_ids: list[str | None] = []
        self._chain = chain
        self._fail = fail

    async def handle(self, event: EventEnvelope, uow: UnitOfWork) -> None:
        self.trace_ids.append(current_trace_id())
        if self._chain:
            outbox = OutboxWriter(uow.session, schema=_SCHEMA, signer=EventSigner(kms_for_tests()))
            await outbox.publish(
                event_type=_CHAINED_TYPE,
                aggregate_type="evidence",
                aggregate_id=event.aggregate_id,
                payload={"evidence_id": str(event.aggregate_id)},
                correlation_id=str(event.correlation_id),
                causation_id=str(event.event_id),
                actor_type="system",
            )
        if self._fail:
            raise RuntimeError("handler exploded")


def _dispatcher(
    db: async_sessionmaker[AsyncSession], recorder: _Recorder, *, event_type: str = _EVENT_TYPE
) -> EventDispatcher:
    """The real relay, in strict signature mode — so delivery also proves verification."""
    dispatcher = EventDispatcher(
        db,
        poll_schemas=(_SCHEMA,),
        lease_seconds=0,
        signer=EventSigner(kms_for_tests()),
        signature_mode=VERIFY_STRICT,
    )
    dispatcher.register(event_type, recorder.handle, inbox_schema=_SCHEMA)
    return dispatcher


async def _publish(db: async_sessionmaker[AsyncSession], **overrides: Any) -> uuid.UUID:
    """Publish through the real writer, so the row is signed exactly as production signs it."""
    aggregate_id = uuid4()
    async with db() as session:
        await OutboxWriter(session, schema=_SCHEMA, signer=EventSigner(kms_for_tests())).publish(
            event_type=_EVENT_TYPE,
            aggregate_type="evidence",
            aggregate_id=aggregate_id,
            payload={"evidence_id": str(aggregate_id)},
            correlation_id=str(uuid4()),
            actor_type="user",
            actor_ref=uuid4(),
            **overrides,
        )
        await session.commit()
    return aggregate_id


async def _rows(db: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with db() as session:
        result = await session.execute(select(get_outbox_table(_SCHEMA)))
        return [dict(row) for row in result.mappings().all()]


def _consumer_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.kind is SpanKind.CONSUMER]


# --- the publisher side -----------------------------------------------------
async def test_publishing_inside_a_span_stamps_the_traceparent(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """The span open when the business transaction ran is the one recorded on the event.

    It has to be captured here: by the time the dispatcher relays the row, that span is closed and
    its context is gone. There is no later point at which this is recoverable.
    """
    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        expected = current_trace_id()
        await _publish(db)

    row = (await _rows(db))[0]
    assert row["trace_id"] is not None
    assert row["trace_id"].startswith("00-")
    assert row["trace_id"].split("-")[1] == expected


async def test_publishing_untraced_leaves_the_column_null(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """No active span, no claim about one — and the value is inside the signature (ADR-0007).

    A zeroed placeholder would be the platform's own key attesting to an execution path that never
    happened, in a record whose purpose is evidentiary provenance.
    """
    await _publish(db)

    assert (await _rows(db))[0]["trace_id"] is None


async def test_an_explicit_trace_id_wins_over_the_ambient_span(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """A replay tool or backfill states its own context; the default must not override it."""
    stated = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        await _publish(db, trace_id=stated)

    assert (await _rows(db))[0]["trace_id"] == stated


async def test_a_signed_event_with_a_traceparent_still_verifies(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """`trace_id` is in the signed field set, so populating it must not break ADR-0007.

    The dispatcher runs in ``VERIFY_STRICT``: a signature that failed to verify would quarantine the
    row as `dead_letter` and never call the handler. Delivery *is* the verification assertion.
    """
    recorder = _Recorder()
    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        await _publish(db)

    await _dispatcher(db, recorder)._poll_once()

    assert len(recorder.trace_ids) == 1, "a forged or unverifiable event never reaches a handler"
    assert (await _rows(db))[0]["dispatch_status"] == "dispatched"


# --- the consumer side ------------------------------------------------------
async def test_the_handler_runs_inside_the_publishing_request_s_trace(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """The whole point of ADR-0018: one trace across the asynchronous boundary.

    Without this the workflow renders in Tempo as an API request that mysteriously ends and
    unrelated background work that mysteriously starts.
    """
    recorder = _Recorder()
    with trace.get_tracer("test").start_as_current_span("POST /evidence") as request_span:
        request_trace = format(request_span.get_span_context().trace_id, "032x")
        request_span_id = format(request_span.get_span_context().span_id, "016x")
        await _publish(db)

    await _dispatcher(db, recorder)._poll_once()

    assert recorder.trace_ids == [request_trace], "the handler logged the request's trace"
    consumer = _consumer_spans(exporter)
    assert len(consumer) == 1
    assert format(consumer[0].context.trace_id, "032x") == request_trace
    assert consumer[0].parent is not None
    assert format(consumer[0].parent.span_id, "016x") == request_span_id, (
        "a child of the publishing span, not merely a span in the same trace"
    )


async def test_the_consumer_span_carries_the_event_it_is_processing(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """Attributes an operator filters on when a trace shows a slow or failing hop."""
    recorder = _Recorder()
    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        await _publish(db)
    event_id = str((await _rows(db))[0]["event_id"])

    await _dispatcher(db, recorder)._poll_once()

    span = _consumer_spans(exporter)[0]
    assert span.name == f"consume {_EVENT_TYPE}"
    assert span.attributes is not None
    assert span.attributes["messaging.message.id"] == event_id
    assert span.attributes["messaging.destination.name"] == _SCHEMA
    assert span.attributes["sentinelai.event_type"] == _EVENT_TYPE


async def test_the_trace_continues_through_an_event_a_handler_publishes(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """Two hops, one trace — which is what makes a whole workflow legible rather than two of them.

    The chained event is stamped from inside the consumer span, so its `traceparent` names the same
    trace and the *handler's* span as parent. That is how `evidence.ingested → ioc_matched →
    correlation_generated` renders as one path.
    """
    recorder = _Recorder(chain=True)
    with trace.get_tracer("test").start_as_current_span("POST /evidence") as request_span:
        request_trace = format(request_span.get_span_context().trace_id, "032x")
        await _publish(db)

    await _dispatcher(db, recorder)._poll_once()

    chained = [row for row in await _rows(db) if row["event_type"] == _CHAINED_TYPE]
    assert len(chained) == 1
    assert chained[0]["trace_id"] is not None
    assert chained[0]["trace_id"].split("-")[1] == request_trace
    consumer_span_id = format(_consumer_spans(exporter)[0].context.span_id, "016x")
    assert chained[0]["trace_id"].split("-")[2] == consumer_span_id, (
        "the chained event's parent is the handler's span, one hop back"
    )


async def test_an_untraced_event_gets_a_root_span(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """A handler still gets a span — the work is worth timing even when nothing caused it."""
    recorder = _Recorder()
    await _publish(db)

    await _dispatcher(db, recorder)._poll_once()

    consumer = _consumer_spans(exporter)
    assert len(consumer) == 1
    assert consumer[0].parent is None, "a root span, not a child of nothing"
    assert recorder.trace_ids[0] is not None, "it is still traced, just not inherited"


async def test_a_malformed_trace_id_does_not_invent_a_parent(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """A signed but unusable `trace_id` means a root span, never the dispatcher's ambient context.

    ``extract`` returns the current context unchanged on malformed input, so a naive implementation
    parents the handler onto whatever the relay happened to be inside — a causal edge that does not
    exist. In a platform built to prove provenance, a fabricated one is the worst available failure.

    The value is *published* rather than written over, so it is signed and verifies: this is the
    "a producer sent us something we cannot parse" case. Tampering with a stored value is a
    different case entirely, and the test below shows what happens to it.

    The dispatcher is driven from inside an unrelated span, which is the only arrangement where a
    wrong implementation would be visible — with no ambient context, "inherit the current context"
    and "start a root span" produce the same result.
    """
    recorder = _Recorder()
    await _publish(db, trace_id="not-a-traceparent")

    with trace.get_tracer("test").start_as_current_span("dispatcher-internals"):
        await _dispatcher(db, recorder)._poll_once()

    consumer = _consumer_spans(exporter)
    assert len(consumer) == 1
    assert consumer[0].parent is None, "not a child of the dispatcher's own span"
    assert recorder.trace_ids[0] != current_trace_id()


async def test_tampering_with_a_stored_trace_id_is_detected_as_forgery(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """`trace_id` is inside the signed envelope, so editing it in the database breaks the signature.

    This was not a design goal of ADR-0018 — it falls out of ADR-0007 already covering the field —
    but it is worth pinning down, because it means the execution path recorded on an event is as
    tamper-evident as its payload. Someone rewriting history to make an event look like it came from
    a different request gets a `dead_letter` row and a `critical` log line, not a quietly relabelled
    trace.

    It also fixes the cost of the choice: `trace_id` cannot be back-filled or corrected in place on
    a signed row. That is the right trade for a field inside an evidentiary attestation.
    """
    recorder = _Recorder()
    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        await _publish(db)

    async with db() as session:
        tampered = update(get_outbox_table(_SCHEMA)).values(
            trace_id="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )
        await session.execute(tampered)
        await session.commit()

    await _dispatcher(db, recorder)._poll_once()

    assert recorder.trace_ids == [], "a tampered event must never reach a handler"
    assert _consumer_spans(exporter) == []
    row = (await _rows(db))[0]
    assert row["dispatch_status"] == "dead_letter", "quarantined, not left to retry"
    assert row["last_error"] is not None and "ADR-0007" in row["last_error"]


async def test_a_failing_handler_marks_its_span_as_an_error(
    db: async_sessionmaker[AsyncSession], exporter: InMemorySpanExporter
) -> None:
    """A span ending OK while its log line said otherwise sends an operator to the wrong place."""
    recorder = _Recorder(fail=True)
    with trace.get_tracer("test").start_as_current_span("POST /evidence"):
        await _publish(db)

    await _dispatcher(db, recorder)._poll_once()

    span = _consumer_spans(exporter)[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.events, "the exception is recorded on the span, not only in the log"
    assert span.events[0].name == "exception"
