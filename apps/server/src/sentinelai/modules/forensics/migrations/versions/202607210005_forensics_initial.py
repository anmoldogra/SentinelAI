"""forensics schema — initial (artifacts + outbox).

Implements database-design.md §3.3. Single rich-record table (``artifacts`` — all
forensic kinds are ``artifact_kind`` values). ``evidence_id`` is a nullable app-ref.
Pure publisher — no ``inbox_events`` (§25.5).

Revision ID: 202607210005_forensics
Revises: (initial)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sentinelai.platform.migrations._event_tables import (
    create_outbox_events,
    drop_outbox_events,
)

revision = "202607210005_forensics"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "forensics"


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("device_info", postgresql.JSONB(), nullable=True),
        sa.Column("acquisition_tool", sa.Text(), nullable=True),
        sa.Column("acquisition_hash", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.create_index("ix_artifacts_evidence_id", "artifacts", ["evidence_id"], schema=_SCHEMA)
    create_outbox_events(_SCHEMA)


def downgrade() -> None:
    drop_outbox_events(_SCHEMA)
    op.drop_index("ix_artifacts_evidence_id", table_name="artifacts", schema=_SCHEMA)
    op.drop_table("artifacts", schema=_SCHEMA)
