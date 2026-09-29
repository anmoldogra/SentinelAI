"""Transactional outbox — guide Part 6, event-driven-architecture.md §16.

Each module owns an ``outbox_events`` table *inside its own schema*. The table
shape is identical across schemas, so it is defined once here as a schema-bound
Core table factory rather than duplicated per module. ``OutboxWriter.publish`` is
called only from within a service method, on the same session the UoW opened —
never a second connection, never after the surrounding ``commit()``. That
same-transaction write is what gives §16's atomicity guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    insert,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.schema import Column

from sentinelai.platform.events.signing import EventSigner
from sentinelai.platform.tracing import current_traceparent

# Dedicated metadata: these generic per-schema tables are created by each module's
# hand-written migration, not by autogenerate against the ORM Base.
event_metadata = MetaData()

_OUTBOX_TABLES: dict[str, Table] = {}


def _build_outbox_table(schema: str) -> Table:
    return Table(
        "outbox_events",
        event_metadata,
        Column("event_id", PGUUID(as_uuid=True), primary_key=True),
        Column("event_type", String(200), nullable=False),
        Column("event_version", String(20), nullable=False),
        Column("aggregate_type", String(100), nullable=False),
        Column("aggregate_id", PGUUID(as_uuid=True), nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("correlation_id", PGUUID(as_uuid=True), nullable=False),
        Column("causation_id", PGUUID(as_uuid=True), nullable=True),
        Column("trace_id", Text, nullable=True),
        Column("actor_type", String(20), nullable=False),
        Column("actor_ref", PGUUID(as_uuid=True), nullable=True),
        Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
        Column("dispatch_status", String(20), nullable=False),
        Column("attempt_count", Integer, nullable=False),
        Column("last_error", Text, nullable=True),
        Column("last_attempted_at", TIMESTAMP(timezone=True), nullable=True),
        # ADR-0007 (Wave 2.3). Nullable: a row written before signing existed genuinely has none,
        # and a verifier must be able to tell that from a signature that fails.
        Column("signature", LargeBinary, nullable=True),
        Column("key_id", Text, nullable=True),
        Column("sig_alg", Text, nullable=True),
        schema=schema,
    )


def get_outbox_table(schema: str) -> Table:
    """Return (memoized) the ``<schema>.outbox_events`` Core table object."""
    table = _OUTBOX_TABLES.get(schema)
    if table is None:
        table = _build_outbox_table(schema)
        _OUTBOX_TABLES[schema] = table
    return table


class OutboxWriter:
    """Writes integration events to one module's outbox, on the module's session.

    **Signing (ADR-0007 §1, Wave 2.3).** When a ``signer`` is present every row is signed under
    ``KeyPurpose.EVENT_ROOT`` before the insert, so the signature lands in the same transaction as
    the business write (event-driven §16). Without one the row is written unsigned, which is the
    honest representation of a publisher that has no signing identity wired — and what the
    dispatcher's strict mode then refuses to deliver, so the gap surfaces loudly at consume time
    rather than silently at publish time.

    ``signer`` is a mutable attribute rather than constructor-only because the dispatcher is the
    composition root for handler UoWs: it builds them through a module-supplied factory that takes
    only a session, and attaching the signer afterwards avoids changing that factory contract in
    every module (see ``EventDispatcher._deliver``).
    """

    def __init__(
        self, session: AsyncSession, schema: str, *, signer: EventSigner | None = None
    ) -> None:
        self._session = session
        self._schema = schema
        self.signer = signer

    async def publish(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        correlation_id: str,
        actor_type: str,
        actor_ref: UUID | None = None,
        causation_id: str | None = None,
        trace_id: str | None = None,
        event_version: str = "1.0.0",
    ) -> None:
        """Insert one ``pending`` outbox row in the caller's open transaction.

        Signed first, then inserted: a signing failure must mean no row rather than an unsigned one,
        and because this runs inside the caller's transaction the whole business write fails with it
        (ADR-0007's fail-closed posture, matching ADR-0003 §1 for the ledgers).

        ``event_id`` and ``occurred_at`` are generated here rather than left to a column default,
        because both are inside the signed message — an event's identity and time are part of what
        the publisher attests to, so they cannot be assigned after the signature is made.

        ``trace_id`` defaults to the **active OTel trace context** as a W3C `traceparent`
        (ADR-0018). This is the only place it can be captured: the span that belongs on the event
        is the one open when the business transaction ran, and by the time the dispatcher relays the
        row minutes later that span is long closed. An explicit argument still wins, so a replay
        tool or a backfill can state its own context — or ``None`` for honestly untraced work.

        It lands inside the signed message with everything else, which is why
        `current_traceparent` returns ``None`` rather than a zeroed placeholder when nothing is
        being traced: a signature is an attestation, and attesting to an execution path that never
        existed would make the envelope say something false about how the evidence moved.
        """
        table = get_outbox_table(self._schema)
        event_id = uuid4()
        occurred_at = datetime.now(UTC)
        trace_id = trace_id if trace_id is not None else current_traceparent()
        signed_fields: dict[str, Any] = {
            "event_id": event_id,
            "event_type": event_type,
            "event_version": event_version,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "payload": payload,
            "correlation_id": UUID(correlation_id)
            if isinstance(correlation_id, str)
            else correlation_id,
            "causation_id": UUID(causation_id) if isinstance(causation_id, str) else causation_id,
            "trace_id": trace_id,
            "actor_type": actor_type,
            "actor_ref": actor_ref,
            "occurred_at": occurred_at,
        }
        signature = (
            await self.signer.sign(schema=self._schema, **signed_fields)
            if self.signer is not None
            else None
        )
        await self._session.execute(
            insert(table).values(
                event_id=event_id,
                event_type=event_type,
                event_version=event_version,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
                trace_id=trace_id,
                actor_type=actor_type,
                actor_ref=actor_ref,
                occurred_at=occurred_at,
                dispatch_status="pending",
                attempt_count=0,
                signature=signature.envelope if signature else None,
                key_id=signature.key_id if signature else None,
                sig_alg=signature.sig_alg if signature else None,
            )
        )
