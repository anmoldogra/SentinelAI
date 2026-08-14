"""threat_intel schema — initial (IOCs, actors, feeds, matches + outbox + inbox).

Implements database-design.md §3.3. This is the one domain-producer that also
CONSUMES (``evidence.ingested``), so it owns an ``inbox_events`` table (§17, §25.4).

Revision ID: 202607210004_threat_intel
Revises: (initial)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sentinelai.platform.migrations._event_tables import (
    create_inbox_events,
    create_outbox_events,
    drop_inbox_events,
    drop_outbox_events,
)

revision = "202607210004_threat_intel"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "threat_intel"


def upgrade() -> None:
    op.create_table(
        "threat_actor_profiles",
        sa.Column("threat_actor_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    op.create_table(
        "iocs",
        sa.Column("ioc_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("indicator_type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("threat_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_seen", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["threat_actor_id"], [f"{_SCHEMA}.threat_actor_profiles.threat_actor_id"]
        ),
        schema=_SCHEMA,
    )
    op.create_index("ix_iocs_evidence_id", "iocs", ["evidence_id"], schema=_SCHEMA)
    op.create_index(
        "ix_iocs_indicator_type_value", "iocs", ["indicator_type", "value"], schema=_SCHEMA
    )
    op.create_table(
        "feed_subscriptions",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("feed_name", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.create_table(
        "ioc_evidence_matches",
        sa.Column("match_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ioc_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matched_evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(["ioc_id"], [f"{_SCHEMA}.iocs.ioc_id"]),
        sa.UniqueConstraint("ioc_id", "matched_evidence_id", name="uq_ioc_match_ioc_evidence"),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ioc_matches_evidence_id",
        "ioc_evidence_matches",
        ["matched_evidence_id"],
        schema=_SCHEMA,
    )
    create_outbox_events(_SCHEMA)
    create_inbox_events(_SCHEMA)


def downgrade() -> None:
    drop_inbox_events(_SCHEMA)
    drop_outbox_events(_SCHEMA)
    op.drop_index("ix_ioc_matches_evidence_id", table_name="ioc_evidence_matches", schema=_SCHEMA)
    op.drop_table("ioc_evidence_matches", schema=_SCHEMA)
    op.drop_table("feed_subscriptions", schema=_SCHEMA)
    op.drop_index("ix_iocs_indicator_type_value", table_name="iocs", schema=_SCHEMA)
    op.drop_index("ix_iocs_evidence_id", table_name="iocs", schema=_SCHEMA)
    op.drop_table("iocs", schema=_SCHEMA)
    op.drop_table("threat_actor_profiles", schema=_SCHEMA)
