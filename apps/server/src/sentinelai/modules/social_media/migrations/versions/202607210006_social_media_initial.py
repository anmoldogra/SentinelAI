"""social_media schema — initial (captured content, observed accounts + outbox).

Implements database-design.md §3.3. ``captured_content.evidence_id`` is a nullable
app-ref. Pure publisher — no ``inbox_events`` (§25.6).

Revision ID: 202607210006_social_media
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

revision = "202607210006_social_media"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "social_media"


def upgrade() -> None:
    op.create_table(
        "captured_content",
        sa.Column("content_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("account_handle", sa.Text(), nullable=False),
        sa.Column("content_kind", sa.Text(), nullable=False),
        sa.Column("raw_attributes", postgresql.JSONB(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_captured_content_evidence_id", "captured_content", ["evidence_id"], schema=_SCHEMA
    )
    op.create_table(
        "social_accounts_observed",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("first_observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    create_outbox_events(_SCHEMA)


def downgrade() -> None:
    drop_outbox_events(_SCHEMA)
    op.drop_table("social_accounts_observed", schema=_SCHEMA)
    op.drop_index("ix_captured_content_evidence_id", table_name="captured_content", schema=_SCHEMA)
    op.drop_table("captured_content", schema=_SCHEMA)
