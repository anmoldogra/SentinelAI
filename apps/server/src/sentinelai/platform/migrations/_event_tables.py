"""Shared DDL for the per-schema ``outbox_events`` / ``inbox_events`` tables.

Every module owns an ``outbox_events`` table; every consuming module also owns an
``inbox_events`` table (event-driven-architecture.md §9 & §17, database-design.md §2).
The shape is identical across schemas and versioned by the event envelope contract,
so it is defined once here and called from each module's initial migration. It
matches the Core table factories in ``platform/events/{outbox,inbox}.py`` exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def create_outbox_events(schema: str) -> None:
    op.create_table(
        "outbox_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column("event_version", sa.String(20), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("dispatch_status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=schema,
    )
    # The dispatcher polls WHERE dispatch_status = 'pending' ORDER BY occurred_at.
    op.create_index(
        f"ix_{schema}_outbox_pending",
        "outbox_events",
        ["dispatch_status", "occurred_at"],
        schema=schema,
    )


# Name of the ADR-0006 dispatcher index, per schema. Kept as a function rather than inlined so the
# eight per-module migrations that add it and the one that documents it cannot drift apart.
def outbox_dispatch_index(schema: str) -> str:
    return f"ix_{schema}_outbox_dispatch"


def create_outbox_dispatch_index(schema: str) -> None:
    """The ADR-0006 dispatcher index: ``(dispatch_status, aggregate_id, occurred_at)``.

    The Wave 1 index ``ix_{schema}_outbox_pending`` covers
    ``(dispatch_status, occurred_at)``, which served a poll that ordered only by time. Wave 2.2's
    claim query groups by aggregate to preserve per-aggregate ordering — it picks the oldest pending
    row *per* ``aggregate_id`` — so ``aggregate_id`` has to sit between the two for the index to
    serve that shape rather than degrading to a filter-then-sort.

    Both indexes are kept. The older one still serves the narrower "is anything pending" question,
    and dropping an index from a table the dispatcher polls under load is a separate decision from
    adding one.
    """
    op.create_index(
        outbox_dispatch_index(schema),
        "outbox_events",
        ["dispatch_status", "aggregate_id", "occurred_at"],
        schema=schema,
    )


def drop_outbox_dispatch_index(schema: str) -> None:
    op.drop_index(outbox_dispatch_index(schema), table_name="outbox_events", schema=schema)


def create_inbox_events(schema: str) -> None:
    op.create_table(
        "inbox_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handler_name", sa.String(200), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("processing_status", sa.String(20), nullable=False),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("event_id", "handler_name"),
        schema=schema,
    )


def drop_outbox_events(schema: str) -> None:
    # Only the index this function's `create_outbox_events` made. The ADR-0006 dispatch index is
    # added by a later migration and dropped by that migration's own `downgrade()`, so touching it
    # here would make a full downgrade-to-base fail on an index that is already gone.
    op.drop_index(f"ix_{schema}_outbox_pending", table_name="outbox_events", schema=schema)
    op.drop_table("outbox_events", schema=schema)


def drop_inbox_events(schema: str) -> None:
    op.drop_table("inbox_events", schema=schema)
