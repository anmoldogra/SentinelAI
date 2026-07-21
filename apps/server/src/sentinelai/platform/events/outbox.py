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
    """Writes integration events to one module's outbox, on the module's session."""

    def __init__(self, session: AsyncSession, schema: str) -> None:
        self._session = session
        self._schema = schema

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
        """Insert one ``pending`` outbox row in the caller's open transaction."""
        table = get_outbox_table(self._schema)
        await self._session.execute(
            insert(table).values(
                event_id=uuid4(),
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
                occurred_at=datetime.now(UTC),
                dispatch_status="pending",
                attempt_count=0,
            )
        )
