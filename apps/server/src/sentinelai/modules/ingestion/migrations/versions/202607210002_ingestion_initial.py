"""ingestion schema — initial (canonical evidence, custody ledger, registries).

Implements database-design.md §3.2 + the module's ``outbox_events`` (§2). ``evidence``
and ``evidence_custody_events`` are append-only; both carry a BRIN index on their
time column (§6). Inter-schema refs (``collector_user_id``, custody ``actor_user_id``)
are plain UUID columns with a B-tree index and NO foreign key (§5).

Revision ID: 202607210002_ingestion
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

revision = "202607210002_ingestion"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("integrity_algorithm", sa.Text(), nullable=True),
        sa.Column("integrity_hash", sa.Text(), nullable=True),
        sa.Column("integrity_verification_status", sa.Text(), nullable=True),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("inline_payload", postgresql.JSONB(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("encoding", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=False),
        sa.Column("reliability_rating", sa.Text(), nullable=True),
        sa.Column("sensitivity", sa.Text(), nullable=True),
        sa.Column("legal_authority_ref", sa.Text(), nullable=True),
        sa.Column("access_restriction_tags", postgresql.JSONB(), nullable=True),
        sa.Column("geo", postgresql.JSONB(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("supersedes_evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("collector_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retention_policy_ref", sa.Text(), nullable=False),
        sa.Column("legal_hold", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["supersedes_evidence_id"], [f"{_SCHEMA}.evidence.evidence_id"]),
        schema=_SCHEMA,
    )
    op.create_index(
        "brin_evidence_ingested_at",
        "evidence",
        ["ingested_at"],
        schema=_SCHEMA,
        postgresql_using="brin",
    )
    op.create_index(
        "ix_evidence_category_artifact_type",
        "evidence",
        ["category", "artifact_type"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_evidence_collector_user_id", "evidence", ["collector_user_id"], schema=_SCHEMA
    )

    op.create_table(
        "evidence_custody_events",
        sa.Column("custody_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("authority_ref", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("integrity_hash_at_event", sa.Text(), nullable=False),
        sa.Column("prev_event_hash", sa.Text(), nullable=False),
        sa.Column("entry_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["evidence_id"], [f"{_SCHEMA}.evidence.evidence_id"]),
        schema=_SCHEMA,
    )
    op.create_index(
        "brin_custody_occurred_at",
        "evidence_custody_events",
        ["occurred_at"],
        schema=_SCHEMA,
        postgresql_using="brin",
    )
    op.create_index(
        "ix_custody_evidence_id", "evidence_custody_events", ["evidence_id"], schema=_SCHEMA
    )

    op.create_table(
        "intake_records",
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connector_name", sa.Text(), nullable=False),
        sa.Column("raw_payload_ref", sa.Text(), nullable=False),
        sa.Column("validation_status", sa.Text(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resulting_evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["resulting_evidence_id"], [f"{_SCHEMA}.evidence.evidence_id"]),
        schema=_SCHEMA,
    )

    op.create_table(
        "connector_registry",
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owning_module", sa.Text(), nullable=False),
        sa.Column("mapping_profile_version", sa.Text(), nullable=False),
        schema=_SCHEMA,
    )

    op.create_table(
        "attribute_schema_registry",
        sa.Column("registry_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "schema_version", "category", "artifact_type", name="uq_attr_schema_ver_cat_type"
        ),
        schema=_SCHEMA,
    )

    create_outbox_events(_SCHEMA)


def downgrade() -> None:
    drop_outbox_events(_SCHEMA)
    op.drop_table("attribute_schema_registry", schema=_SCHEMA)
    op.drop_table("connector_registry", schema=_SCHEMA)
    op.drop_table("intake_records", schema=_SCHEMA)
    op.drop_index("ix_custody_evidence_id", table_name="evidence_custody_events", schema=_SCHEMA)
    op.drop_index("brin_custody_occurred_at", table_name="evidence_custody_events", schema=_SCHEMA)
    op.drop_table("evidence_custody_events", schema=_SCHEMA)
    op.drop_index("ix_evidence_collector_user_id", table_name="evidence", schema=_SCHEMA)
    op.drop_index("ix_evidence_category_artifact_type", table_name="evidence", schema=_SCHEMA)
    op.drop_index("brin_evidence_ingested_at", table_name="evidence", schema=_SCHEMA)
    op.drop_table("evidence", schema=_SCHEMA)
