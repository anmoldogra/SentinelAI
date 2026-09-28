"""Outbox relay — event-driven-architecture.md §2, §14-15, §18; ADR-0006.

Phase 1 transport: a poller that relays each module's ``outbox_events`` rows to registered handlers.
Phase 3+ replaces this relay half with a Redpanda producer/consumer — the outbox write, envelope,
inbox check, and event catalog do NOT change, only the transport between "row written" and "handler
invoked".

**Runs in the worker, not the API (ADR-0006 §1).** It used to start in every HTTP replica's
lifespan, which meant N replicas ran N uncoordinated pollers over the same tables: duplicate
delivery (masked by inbox dedup, not prevented), wasted database load, and no ordering guarantee.

**Claiming, not reading (ADR-0006 §2).** The poll takes ``FOR UPDATE SKIP LOCKED`` and stamps
``last_attempted_at`` inside the claim transaction. The row lock alone is not enough: it is released
at commit, and a row left ``pending`` while its handlers run would be picked up again by the next
poll — the double-dispatch this is meant to prevent. The stamp turns the claim into a **lease**, and
the claim query skips rows leased within :data:`CLAIM_LEASE_SECONDS`.

The lease is what makes a crashed dispatcher safe. A row it claimed and never finished simply
becomes claimable again when the lease expires, with no reaper process and no ``dispatching`` status
that could strand rows if the process holding them died. At-least-once is preserved exactly as
before, and duplicate delivery after a crash is absorbed by each handler's Inbox guard, as always.

**Per-aggregate ordering (ADR-0006 §3).** The claim takes the *oldest pending row per*
``aggregate_id`` — never two rows for the same aggregate in one batch. Combined with the lease that
is what gives strict ordering across dispatchers: while event 1 for aggregate X is in flight it is
still ``pending`` and still leased, so it remains the oldest pending row for X and X yields nothing.
Event 2 for X cannot be claimed until event 1 resolves. No advisory lock, no schema hash-partition —
the ordering falls out of what the claim is allowed to select.

**Backoff in the query (ADR-0006 §4).** The same ``last_attempted_at`` gate that implements the
lease implements retry backoff: a failed row is not reconsidered until its lease window passes, so
retries cannot hot-loop.

Also honored, unchanged from Phase 1:
- **Per-module poll** — each schema is drained independently, so one module's backlog never delays
  another's dispatch.
- **At-least-once** — a handler is invoked in its own transaction; the source row is marked
  ``dispatched`` only after all handlers succeed, and ``dead_letter`` at ``max_attempts``.
- **Graceful shutdown** — ``request_shutdown()`` lets the current drain finish and stops between
  rows; it never aborts a handler mid-transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinelai.platform.crypto.metrics import (
    EVENT_SIGNATURE_FAILURES,
    EVENT_SIGNATURES_VERIFIED,
)
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.outbox import get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from sentinelai.platform.logging import log

# Schema→module map (database-design.md §2): every module owns an outbox_events
# table; `platform` does not publish onto the event bus in Phase 1.
DEFAULT_OUTBOX_SCHEMAS: tuple[str, ...] = (
    "ingestion",
    "osint",
    "threat_intel",
    "forensics",
    "social_media",
    "case_management",
    "investigation",
    "notification",
)

# How long a claimed row stays invisible to other dispatchers, and equally the minimum gap between
# retry attempts (ADR-0006 §2/§4 are one mechanism here). Long enough that a normal handler finishes
# well inside it; short enough that a dispatcher killed mid-batch does not strand its rows for long.
CLAIM_LEASE_SECONDS: int = 60

# ADR-0007 §2 verification outcomes.
VERIFY_STRICT: Final = "strict"
VERIFY_PERMISSIVE: Final = "permissive"

EventHandler = Callable[[EventEnvelope, UnitOfWork], Awaitable[None]]
# A module supplies its own concrete UoW type as the factory so its handler gets
# the module's repositories + outbox. platform stays agnostic — it only calls it.
UowFactory = Callable[[AsyncSession], UnitOfWork]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Dispatch retry bounds. Numeric defaults mirror §14's "Standard" policy and
    are tunable per deployment, not a hard contract."""

    max_attempts: int = 5


@dataclass(slots=True)
class _Registration:
    handler: EventHandler
    inbox_schema: str
    uow_factory: UowFactory = UnitOfWork
    policy: RetryPolicy = field(default_factory=RetryPolicy)


class EventDispatcher:
    """Polls every module's outbox and invokes registered handlers by event type."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_schemas: Sequence[str] = DEFAULT_OUTBOX_SCHEMAS,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 100,
        lease_seconds: int = CLAIM_LEASE_SECONDS,
        signer: EventSigner | None = None,
        signature_mode: str = VERIFY_STRICT,
    ) -> None:
        self._session_factory = session_factory
        self._poll_schemas = tuple(poll_schemas)
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        # ADR-0007 §2. With no signer the dispatcher cannot verify at all, so it does not pretend
        # to:
        # verification is skipped and that is logged once at startup rather than per event. A
        # deployment reaches that state only by not wiring a KMS into the relay.
        self._signer = signer
        self._signature_mode = signature_mode
        self._handlers: dict[str, list[_Registration]] = defaultdict(list)
        self._shutdown = asyncio.Event()

    # -- registration -------------------------------------------------------
    def register[U: UnitOfWork](
        self,
        event_type: str,
        handler: Callable[[EventEnvelope, U], Awaitable[None]],
        *,
        inbox_schema: str,
        uow_factory: Callable[[AsyncSession], U] | None = None,
        policy: RetryPolicy | None = None,
    ) -> None:
        """Subscribe a handler to an event type. Called from the composition root.

        Generic over the module's concrete ``UnitOfWork`` subtype: the handler and the
        ``uow_factory`` that produces its argument are bound to the same ``U`` so a module can
        register a handler typed on its own UoW. Registrations are stored under the base types
        (they are invoked uniformly by the poll loop), hence the internal casts.
        """
        self._handlers[event_type].append(
            _Registration(
                handler=cast(EventHandler, handler),
                inbox_schema=inbox_schema,
                uow_factory=cast(UowFactory, uow_factory)
                if uow_factory is not None
                else UnitOfWork,
                policy=policy or RetryPolicy(),
            )
        )

    # -- lifecycle ----------------------------------------------------------
    def request_shutdown(self) -> None:
        """Signal the poll loop to stop after the current drain (graceful)."""
        self._shutdown.set()

    async def run_forever(self) -> None:
        """Poll loop. Runs until ``request_shutdown()`` and the current drain ends."""
        log.info(
            "event_dispatcher_started",
            schemas=list(self._poll_schemas),
            signature_mode=self._signature_mode if self._signer else "disabled (no signer wired)",
        )
        while not self._shutdown.is_set():
            try:
                dispatched = await self._poll_once()
            except Exception:  # never let one bad cycle kill the dispatcher
                log.exception("event_dispatcher_poll_failed")
                dispatched = 0
            if dispatched == 0:
                await self._wait(self._poll_interval)
        log.info("event_dispatcher_stopped")

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)

    # -- polling ------------------------------------------------------------
    async def _poll_once(self) -> int:
        total = 0
        for schema in self._poll_schemas:
            if self._shutdown.is_set():
                break
            total += await self._drain_schema(schema)
        return total

    async def _drain_schema(self, schema: str) -> int:
        rows = await self._claim_batch(schema)

        processed = 0
        for row in rows:
            if self._shutdown.is_set():
                break
            await self._process_row(schema, EventEnvelope.from_row(row))
            processed += 1
        return processed

    async def _claim_batch(self, schema: str) -> Sequence[RowMapping]:
        """Claim up to ``batch_size`` rows: one per aggregate, locked, leased — ADR-0006 §2/§3/§4.

        The claim and the lease stamp share one transaction. If they did not, a dispatcher could
        select rows, lose its connection before stamping, and leave them looking claimable while it
        went on to dispatch them.

        ``DISTINCT ON (aggregate_id)`` sits in a subquery because Postgres rejects ``SELECT DISTINCT
        ... FOR UPDATE`` outright. The subquery picks the oldest pending row per aggregate; the
        outer statement locks exactly those rows and skips any a peer already holds.
        """
        table = get_outbox_table(schema)
        now = datetime.now(UTC)
        lease_cutoff = now - timedelta(seconds=self._lease_seconds)

        claimable = (
            select(table.c.event_id)
            .distinct(table.c.aggregate_id)
            .where(
                table.c.dispatch_status == "pending",
                # Never claimed, or its lease has expired. This is simultaneously the
                # double-dispatch guard and the retry backoff (ADR-0006 §4).
                or_(
                    table.c.last_attempted_at.is_(None),
                    table.c.last_attempted_at < lease_cutoff,
                ),
            )
            # `aggregate_id` first because DISTINCT ON requires it to lead; `occurred_at` is what
            # makes the surviving row per aggregate the OLDEST one, which is the ordering guarantee.
            .order_by(table.c.aggregate_id, table.c.occurred_at.asc())
            .subquery()
        )

        async with self._session_factory() as session:
            locked = await session.execute(
                select(table)
                .where(table.c.event_id.in_(select(claimable.c.event_id)))
                .order_by(table.c.occurred_at.asc())
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = locked.mappings().all()
            if not rows:
                return rows
            # Stamping inside the claim transaction is what converts a row lock (released at commit)
            # into a lease that outlives it.
            await session.execute(
                update(table)
                .where(table.c.event_id.in_([row["event_id"] for row in rows]))
                .values(last_attempted_at=now)
            )
            await session.commit()
        return rows

    async def _process_row(self, schema: str, event: EventEnvelope) -> None:
        # ADR-0007 §2: verify BEFORE any handler, and before the inbox claim. A forged event that
        # reached a handler would already have had its effect by the time anything noticed, and the
        # inbox cannot help — it deduplicates on `(event_id, handler_name)`, and a forger mints a
        # fresh id.
        if not await self._verify(schema, event):
            return

        registrations = self._handlers.get(event.event_type, [])
        all_succeeded = True
        for registration in registrations:
            if not await self._deliver(event, registration):
                all_succeeded = False

        if all_succeeded:
            await self._mark(schema, event, status="dispatched")
            return

        next_attempt = event.attempt_count + 1
        max_attempts = max((r.policy.max_attempts for r in registrations), default=1)
        if next_attempt >= max_attempts:
            await self._mark(schema, event, status="dead_letter", attempt_count=next_attempt)
            log.error(
                "event_dead_lettered", event_id=str(event.event_id), event_type=event.event_type
            )
        else:
            await self._mark(schema, event, status="pending", attempt_count=next_attempt)

    async def _verify(self, schema: str, event: EventEnvelope) -> bool:
        """Whether this event is authentic enough to deliver — ADR-0007 §2.

        Three outcomes, and the middle one is why a single boolean on the signature is not enough:

        * **verified** — deliver.
        * **absent** — deliver only under ``permissive``. A row written before Wave 2.3, or by a
          publisher with no signing identity wired, genuinely has no signature. It cannot be
          signed retroactively with any honesty, so a deployment migrating real data needs a window
          in which such rows still flow. Strict refuses them.
        * **invalid** — never delivered, in either mode. A signature that is present and does not
          verify is an active forgery attempt, not unproven history. Tolerating it under permissive
          would hand an attacker a downgrade: corrupt the envelope and the event is treated as
          merely unsigned.

        A rejected event is quarantined as ``dead_letter`` immediately rather than retried. Retrying
        is for transient failures, and a bad signature will not become good — retrying would only
        delay the alarm while burning attempts.
        """
        if self._signer is None:
            return True  # nothing to verify with; logged once at startup

        if event.signature is None:
            if self._signature_mode == VERIFY_PERMISSIVE:
                EVENT_SIGNATURE_FAILURES.labels(schema=schema, reason="missing_tolerated").inc()
                log.warning(
                    "event_unsigned_tolerated",
                    event_id=str(event.event_id),
                    event_type=event.event_type,
                    schema=schema,
                    detail="permissive mode: delivered without authentication (ADR-0007 §2)",
                )
                return True
            await self._quarantine(schema, event, reason="signature missing")
            EVENT_SIGNATURE_FAILURES.labels(schema=schema, reason="missing").inc()
            return False

        valid = await self._signer.verify(
            schema=schema,
            envelope=event.signature,
            event_id=event.event_id,
            event_type=event.event_type,
            event_version=event.event_version,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            trace_id=event.trace_id,
            actor_type=event.actor_type,
            actor_ref=event.actor_ref,
            occurred_at=event.occurred_at,
        )
        if not valid:
            await self._quarantine(schema, event, reason="signature invalid")
            EVENT_SIGNATURE_FAILURES.labels(schema=schema, reason="invalid").inc()
            return False

        EVENT_SIGNATURES_VERIFIED.labels(schema=schema).inc()
        return True

    async def _quarantine(self, schema: str, event: EventEnvelope, *, reason: str) -> None:
        """Terminally reject an event that failed authentication, and say so loudly.

        ``CRITICAL`` rather than a warning: under the platform's threat model a forged event is an
        insider with write access to a module's schema attempting to fabricate a domain fact
        (ADR-0007 Context). That is a security incident, not a delivery hiccup.
        """
        await self._mark(schema, event, status="dead_letter", last_error=f"ADR-0007: {reason}")
        log.critical(
            "event_signature_rejected",
            event_id=str(event.event_id),
            event_type=event.event_type,
            aggregate_id=str(event.aggregate_id),
            schema=schema,
            reason=reason,
        )

    async def _deliver(self, event: EventEnvelope, registration: _Registration) -> bool:
        """Invoke one handler in its own transaction. True on success, False on failure."""
        try:
            async with self._session_factory() as session:
                uow = registration.uow_factory(session)
                # The factory takes only a session (a contract every module implements), so the
                # signer is attached here instead — otherwise an event published BY a handler, such
                # as `notification.dispatched`, would be written unsigned and then refused by this
                # same dispatcher on the next poll.
                outbox = getattr(uow, "outbox", None)
                if outbox is not None and getattr(outbox, "signer", None) is None:
                    outbox.signer = self._signer
                await registration.handler(event, uow)
                await uow.commit()
            return True
        except Exception:
            log.exception(
                "event_handler_failed",
                event_id=str(event.event_id),
                event_type=event.event_type,
                inbox_schema=registration.inbox_schema,
            )
            return False

    async def _mark(
        self,
        schema: str,
        event: EventEnvelope,
        *,
        status: str,
        attempt_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        table = get_outbox_table(schema)
        values: dict[str, object] = {"dispatch_status": status}
        if last_error is not None:
            # Persisted on the row, so an operator investigating a dead-lettered event does not have
            # to correlate it against a log line to learn why it was rejected.
            values["last_error"] = last_error
        if attempt_count is not None:
            values["attempt_count"] = attempt_count
            # Re-stamped so the backoff window runs from THIS failure, not from the claim. A retry
            # that inherited the claim stamp would become eligible again almost immediately.
            values["last_attempted_at"] = datetime.now(UTC)
        async with self._session_factory() as session:
            await session.execute(
                update(table).where(table.c.event_id == event.event_id).values(**values)
            )
            await session.commit()
