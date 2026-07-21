"""Inbox deduplication — guide Part 6, event-driven-architecture.md §17.

At-least-once delivery is assumed always. Every consumer performs the Inbox claim
(insert-first on the composite ``(event_id, handler_name)`` key) BEFORE any side
effect. A duplicate delivery fails the insert and the handler skips — no side
effect re-runs. Each consuming module owns an ``inbox_events`` table in its schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, Table, Text, insert, update
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.schema import Column

from sentinelai.platform.events.outbox import event_metadata

_INBOX_TABLES: dict[str, Table] = {}


def _build_inbox_table(schema: str) -> Table:
    return Table(
        "inbox_events",
        event_metadata,
        Column("event_id", PGUUID(as_uuid=True), primary_key=True),
        Column("handler_name", String(200), primary_key=True),
        Column("received_at", TIMESTAMP(timezone=True), nullable=False),
        Column("processing_status", String(20), nullable=False),
        Column("processed_at", TIMESTAMP(timezone=True), nullable=True),
        Column("last_error", Text, nullable=True),
        schema=schema,
    )


def get_inbox_table(schema: str) -> Table:
    """Return (memoized) the ``<schema>.inbox_events`` Core table object."""
    table = _INBOX_TABLES.get(schema)
    if table is None:
        table = _build_inbox_table(schema)
        _INBOX_TABLES[schema] = table
    return table


class InboxGuard:
    """Insert-first idempotency guard for one consuming module's handlers."""

    def __init__(self, session: AsyncSession, schema: str) -> None:
        self._session = session
        self._schema = schema

    async def try_claim(self, event_id: UUID, handler_name: str) -> bool:
        """Claim ``(event_id, handler_name)``. True on first delivery, False on redelivery."""
        table = get_inbox_table(self._schema)
        savepoint = await self._session.begin_nested()
        try:
            await self._session.execute(
                insert(table).values(
                    event_id=event_id,
                    handler_name=handler_name,
                    received_at=datetime.now(UTC),
                    processing_status="processing",
                )
            )
        except IntegrityError:
            await savepoint.rollback()
            return False  # already claimed by this handler — redelivery, skip
        return True

    async def mark_processed(self, event_id: UUID, handler_name: str) -> None:
        """Mark a claimed inbox row processed after the handler's side effects commit."""
        table = get_inbox_table(self._schema)
        await self._session.execute(
            update(table)
            .where(table.c.event_id == event_id, table.c.handler_name == handler_name)
            .values(processing_status="processed", processed_at=datetime.now(UTC))
        )
