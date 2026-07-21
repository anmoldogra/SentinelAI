"""investigation schema — initial (entities, relationships, revisions, links, runs).

Implements database-design.md §3.5 + outbox/inbox (consumes evidence.ingested,
evidence.linked/unlinked_to_case, threat_intel.ioc_matched — §25.8). ``entity_revisions``
and ``relationship_revisions`` are append-only; ``relationship_evidence`` and
``entity_evidence_mentions`` carry the mandatory supporting-evidence app-refs (CEM §13).

Revision ID: 202607210008_investigation
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

revision = "202607210008_investigation"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "investigation"


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=False),
        sa.Column("created_by_type", sa.String(20), nullable=False),
        sa.Column("created_by_ref", postgresql.UUID(as_uuid=True), nullable=False),
        schema=_SCHEMA,
    )
    op.create_table(
        "entity_revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_changed", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("changed_by_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], [f"{_SCHEMA}.entities.entity_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_entity_revisions_entity_id", "entity_revisions", ["entity_id"], schema=_SCHEMA)
    op.create_table(
        "relationships",
        sa.Column("relationship_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("from_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("directional", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=False),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by_type", sa.String(20), nullable=False),
        sa.Column("created_by_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["from_entity_id"], [f"{_SCHEMA}.entities.entity_id"]),
        sa.ForeignKeyConstraint(["to_entity_id"], [f"{_SCHEMA}.entities.entity_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_relationships_from_entity", "relationships", ["from_entity_id"], schema=_SCHEMA)
    op.create_index("ix_relationships_to_entity", "relationships", ["to_entity_id"], schema=_SCHEMA)
    op.create_table(
        "relationship_revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("relationship_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(20), nullable=False),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["relationship_id"], [f"{_SCHEMA}.relationships.relationship_id"]
        ),
        schema=_SCHEMA,
    )
    op.create_table(
        "relationship_evidence",
        sa.Column("relationship_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("relationship_id", "evidence_id"),
        sa.ForeignKeyConstraint(
            ["relationship_id"], [f"{_SCHEMA}.relationships.relationship_id"]
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_rel_evidence_evidence_id", "relationship_evidence", ["evidence_id"], schema=_SCHEMA
    )
    op.create_table(
        "entity_evidence_mentions",
        sa.Column("mention_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], [f"{_SCHEMA}.entities.entity_id"]),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_entity_mentions_entity_id", "entity_evidence_mentions", ["entity_id"], schema=_SCHEMA
    )
    op.create_index(
        "ix_entity_mentions_evidence_id", "entity_evidence_mentions", ["evidence_id"], schema=_SCHEMA
    )
    op.create_table(
        "correlation_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("findings_generated_count", sa.Integer(), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_index("ix_correlation_runs_case_id", "correlation_runs", ["case_id"], schema=_SCHEMA)
    create_outbox_events(_SCHEMA)
    create_inbox_events(_SCHEMA)


def downgrade() -> None:
    drop_inbox_events(_SCHEMA)
    drop_outbox_events(_SCHEMA)
    op.drop_index("ix_correlation_runs_case_id", table_name="correlation_runs", schema=_SCHEMA)
    op.drop_table("correlation_runs", schema=_SCHEMA)
    op.drop_index("ix_entity_mentions_evidence_id", table_name="entity_evidence_mentions", schema=_SCHEMA)
    op.drop_index("ix_entity_mentions_entity_id", table_name="entity_evidence_mentions", schema=_SCHEMA)
    op.drop_table("entity_evidence_mentions", schema=_SCHEMA)
    op.drop_index("ix_rel_evidence_evidence_id", table_name="relationship_evidence", schema=_SCHEMA)
    op.drop_table("relationship_evidence", schema=_SCHEMA)
    op.drop_table("relationship_revisions", schema=_SCHEMA)
    op.drop_index("ix_relationships_to_entity", table_name="relationships", schema=_SCHEMA)
    op.drop_index("ix_relationships_from_entity", table_name="relationships", schema=_SCHEMA)
    op.drop_table("relationships", schema=_SCHEMA)
    op.drop_index("ix_entity_revisions_entity_id", table_name="entity_revisions", schema=_SCHEMA)
    op.drop_table("entity_revisions", schema=_SCHEMA)
    op.drop_table("entities", schema=_SCHEMA)
