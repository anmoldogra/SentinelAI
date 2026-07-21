"""In-process event dispatcher — event-driven-architecture.md §2, §14–15.

Phase 1 transport: a single in-process poller that relays each module's
``outbox_events`` rows to registered handlers. Phase 3+ replaces this relay half
with a Redpanda producer/consumer — the outbox write, envelope, inbox check, and
event catalog do NOT change, only the transport between "row written" and
"handler invoked".

Design points honored here:
- **Per-module poll** — each schema is drained independently, so one module's
  backlog never delays another's dispatch.
- **At-least-once** — a handler is invoked in its own transaction; the source
  outbox row is marked ``dispatched`` only after all handlers succeed. On failure
  the row stays ``pending`` (retry) until ``max_attempts`` → ``dead_letter``.
  Duplicate delivery on retry is absorbed by each handler's Inbox guard.
- **Graceful shutdown** — ``request_shutdown()`` lets the current drain finish and
  stops between rows; it never aborts a handler mid-transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.outbox import get_outbox_table
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
    ) -> None:
        self._session_factory = session_factory
        self._poll_schemas = tuple(poll_schemas)
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._handlers: dict[str, list[_Registration]] = defaultdict(list)
        self._shutdown = asyncio.Event()

    # -- registration -------------------------------------------------------
    def register(
        self,
        event_type: str,
        handler: EventHandler,
        *,
        inbox_schema: str,
        uow_factory: UowFactory = UnitOfWork,
        policy: RetryPolicy | None = None,
    ) -> None:
        """Subscribe a handler to an event type. Called from the composition root."""
        self._handlers[event_type].append(
            _Registration(
                handler=handler,
                inbox_schema=inbox_schema,
                uow_factory=uow_factory,
                policy=policy or RetryPolicy(),
            )
        )

    # -- lifecycle ----------------------------------------------------------
    def request_shutdown(self) -> None:
        """Signal the poll loop to stop after the current drain (graceful)."""
        self._shutdown.set()

    async def run_forever(self) -> None:
        """Poll loop. Runs until ``request_shutdown()`` and the current drain ends."""
        log.info("event_dispatcher_started", schemas=list(self._poll_schemas))
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
        table = get_outbox_table(schema)
        async with self._session_factory() as read_session:
            result = await read_session.execute(
                select(table)
                .where(table.c.dispatch_status == "pending")
                .order_by(table.c.occurred_at.asc())
                .limit(self._batch_size)
            )
            rows = result.mappings().all()

        processed = 0
        for row in rows:
            if self._shutdown.is_set():
                break
            await self._process_row(schema, EventEnvelope.from_row(row))
            processed += 1
        return processed

    async def _process_row(self, schema: str, event: EventEnvelope) -> None:
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
            log.error("event_dead_lettered", event_id=str(event.event_id), event_type=event.event_type)
        else:
            await self._mark(schema, event, status="pending", attempt_count=next_attempt)

    async def _deliver(self, event: EventEnvelope, registration: _Registration) -> bool:
        """Invoke one handler in its own transaction. True on success, False on failure."""
        try:
            async with self._session_factory() as session:
                uow = registration.uow_factory(session)
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
    ) -> None:
        table = get_outbox_table(schema)
        values: dict[str, object] = {"dispatch_status": status}
        if attempt_count is not None:
            values["attempt_count"] = attempt_count
            values["last_attempted_at"] = datetime.now(UTC)
        async with self._session_factory() as session:
            await session.execute(
                update(table).where(table.c.event_id == event.event_id).values(**values)
            )
            await session.commit()
