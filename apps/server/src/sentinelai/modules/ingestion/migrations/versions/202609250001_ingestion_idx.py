"""ingestion schema — dispatcher claim index for ADR-0006 (Wave 2.2).

Adds ``(dispatch_status, aggregate_id, occurred_at)`` on ``ingestion.outbox_events``.

Wave 2.2 replaced the dispatcher's poll with a claim query that preserves per-aggregate ordering:
it takes the *oldest pending row per* ``aggregate_id`` under ``FOR UPDATE SKIP LOCKED``. The
existing ``ix_ingestion_outbox_pending`` index covers ``(dispatch_status, occurred_at)``, which
serves a time-only ordering; with ``aggregate_id`` in the middle the same index serves the grouped
claim instead of degrading into a scan-then-sort as the outbox grows.

One migration per module rather than one reaching across every schema: each module owns its schema
and its own Alembic chain (database-design.md §5), and the ArgoCD PreSync job applies those chains
separately (deployment-architecture.md Part 5).

Alembic stores ``version_num`` in a ``varchar(32)``, so a revision id longer than that fails
the UPDATE at the end of the migration rather than anything in the migration itself. Hence the
terse ``_idx`` suffix.

Revision ID: 202609250001_ingestion_idx
Revises: 202609080004_ingestion_chain
"""

from __future__ import annotations

from sentinelai.platform.migrations._event_tables import (
    create_outbox_dispatch_index,
    drop_outbox_dispatch_index,
)

revision = "202609250001_ingestion_idx"
down_revision = "202609080004_ingestion_chain"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"


def upgrade() -> None:
    create_outbox_dispatch_index(_SCHEMA)


def downgrade() -> None:
    drop_outbox_dispatch_index(_SCHEMA)
