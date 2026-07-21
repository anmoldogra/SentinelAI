"""osint schema — initial (sources, findings, connector state + outbox).

Implements database-design.md §3.3. ``osint_findings.evidence_id`` is a nullable
app-ref (set on publish); ``collected_at`` gets a BRIN index (§6). Pure publisher —
no ``inbox_events`` (§25.3).

Revision ID: 202607210003_osint
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

revision = "202607210003_osint"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "osint"


def upgrade() -> None:
    op.create_table(
        "osint_sources",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("connector_type", sa.Text(), nullable=False),
        sa.Column("reliability_baseline", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_table(
        "osint_findings",
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw_attributes", postgresql.JSONB(), nullable=False),
        sa.Column("reliability_rating", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], [f"{_SCHEMA}.osint_sources.source_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_osint_findings_source_id", "osint_findings", ["source_id"], schema=_SCHEMA)
    op.create_index("ix_osint_findings_evidence_id", "osint_findings", ["evidence_id"], schema=_SCHEMA)
    op.create_index(
        "brin_osint_findings_collected_at", "osint_findings", ["collected_at"],
        schema=_SCHEMA, postgresql_using="brin",
    )
    op.create_table(
        "osint_connector_state",
        sa.Column("state_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cursor", postgresql.JSONB(), nullable=True),
        sa.Column("last_polled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], [f"{_SCHEMA}.osint_sources.source_id"]),
        schema=_SCHEMA,
    )
    create_outbox_events(_SCHEMA)


def downgrade() -> None:
    drop_outbox_events(_SCHEMA)
    op.drop_table("osint_connector_state", schema=_SCHEMA)
    op.drop_index("brin_osint_findings_collected_at", table_name="osint_findings", schema=_SCHEMA)
    op.drop_index("ix_osint_findings_evidence_id", table_name="osint_findings", schema=_SCHEMA)
    op.drop_index("ix_osint_findings_source_id", table_name="osint_findings", schema=_SCHEMA)
    op.drop_table("osint_findings", schema=_SCHEMA)
    op.drop_table("osint_sources", schema=_SCHEMA)
