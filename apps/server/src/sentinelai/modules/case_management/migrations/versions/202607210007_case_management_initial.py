"""case_management schema — initial (cases, evidence links, status history, reports).

Implements database-design.md §3.4 + outbox/inbox (consumes investigation.finding_reviewed,
§25.7). ``case_status_history`` is append-only. Intra-schema FKs to ``cases`` only;
``owning_user_id``/``evidence_id``/actor refs are app-refs (no FK, §5).

Revision ID: 202607210007_case_management
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

revision = "202607210007_case_management"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "case_management"


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("owning_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.create_index("ix_cases_owning_user_id", "cases", ["owning_user_id"], schema=_SCHEMA)
    op.create_table(
        "case_evidence_links",
        sa.Column("link_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("linked_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("linked_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], [f"{_SCHEMA}.cases.case_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_case_links_case_id", "case_evidence_links", ["case_id"], schema=_SCHEMA)
    op.create_index(
        "ix_case_links_evidence_id", "case_evidence_links", ["evidence_id"], schema=_SCHEMA
    )
    op.create_table(
        "case_status_history",
        sa.Column("history_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(50), nullable=False),
        sa.Column("new_status", sa.String(50), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], [f"{_SCHEMA}.cases.case_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_case_history_case_id", "case_status_history", ["case_id"], schema=_SCHEMA)
    op.create_table(
        "case_reports",
        sa.Column("report_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("generated_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], [f"{_SCHEMA}.cases.case_id"]),
        schema=_SCHEMA,
    )
    op.create_index("ix_case_reports_case_id", "case_reports", ["case_id"], schema=_SCHEMA)
    create_outbox_events(_SCHEMA)
    create_inbox_events(_SCHEMA)


def downgrade() -> None:
    drop_inbox_events(_SCHEMA)
    drop_outbox_events(_SCHEMA)
    op.drop_index("ix_case_reports_case_id", table_name="case_reports", schema=_SCHEMA)
    op.drop_table("case_reports", schema=_SCHEMA)
    op.drop_index("ix_case_history_case_id", table_name="case_status_history", schema=_SCHEMA)
    op.drop_table("case_status_history", schema=_SCHEMA)
    op.drop_index("ix_case_links_evidence_id", table_name="case_evidence_links", schema=_SCHEMA)
    op.drop_index("ix_case_links_case_id", table_name="case_evidence_links", schema=_SCHEMA)
    op.drop_table("case_evidence_links", schema=_SCHEMA)
    op.drop_index("ix_cases_owning_user_id", table_name="cases", schema=_SCHEMA)
    op.drop_table("cases", schema=_SCHEMA)
