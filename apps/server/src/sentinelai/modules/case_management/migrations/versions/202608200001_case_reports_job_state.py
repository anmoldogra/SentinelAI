"""case_management — make ``case_reports`` a job-state row (api-design.md §7).

``POST /cases/{id}/reports`` must create the row immediately in ``queued`` state so the client has
something to poll at ``GET /reports/{report_id}``; the original schema made that impossible by
requiring ``storage_ref``/``generated_at`` NOT NULL, neither of which exists until the background
job finishes. This relaxes both to NULL and adds the documented lifecycle columns.

Backfill: existing rows (if any) predate async generation and are, by definition, finished — they
are stamped ``completed`` with ``requested_at`` seeded from ``generated_at`` before the NOT NULL
constraint on ``requested_at`` is applied, so the migration is safe on a populated table.

Revision ID: 202608200001_case_reports_job
Revises: 202607210007_case_management
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "202608200001_case_reports_job"
down_revision = "202607210007_case_management"
branch_labels = None
depends_on = None

_SCHEMA = "case_management"
_TABLE = "case_reports"


def upgrade() -> None:
    # A completed report has an object and a completion time; a queued one has neither.
    op.alter_column(_TABLE, "storage_ref", existing_type=sa.Text(), nullable=True, schema=_SCHEMA)
    op.alter_column(
        _TABLE,
        "generated_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=True,
        schema=_SCHEMA,
    )

    # Added nullable, backfilled, then constrained — never a bare NOT NULL add on a live table.
    op.add_column(_TABLE, sa.Column("status", sa.String(20), nullable=True), schema=_SCHEMA)
    op.add_column(
        _TABLE,
        sa.Column("requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(_TABLE, sa.Column("failure_reason", sa.Text(), nullable=True), schema=_SCHEMA)
    op.execute(
        f"UPDATE {_SCHEMA}.{_TABLE} "
        "SET status = 'completed', requested_at = COALESCE(generated_at, now()) "
        "WHERE status IS NULL"
    )
    op.alter_column(_TABLE, "status", existing_type=sa.String(20), nullable=False, schema=_SCHEMA)
    op.alter_column(
        _TABLE,
        "requested_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
        schema=_SCHEMA,
    )

    # Polling a case's reports is the hot read; keep it index-supported.
    op.create_index("ix_case_reports_case_status", _TABLE, ["case_id", "status"], schema=_SCHEMA)


def downgrade() -> None:
    """Exact inverse. Unfinished reports have no object and cannot satisfy the restored NOT NULL
    constraints, so they are deleted — they reference nothing and are not recoverable state."""
    op.drop_index("ix_case_reports_case_status", table_name=_TABLE, schema=_SCHEMA)
    op.execute(f"DELETE FROM {_SCHEMA}.{_TABLE} WHERE storage_ref IS NULL OR generated_at IS NULL")
    op.drop_column(_TABLE, "failure_reason", schema=_SCHEMA)
    op.drop_column(_TABLE, "requested_at", schema=_SCHEMA)
    op.drop_column(_TABLE, "status", schema=_SCHEMA)
    op.alter_column(
        _TABLE,
        "generated_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
        schema=_SCHEMA,
    )
    op.alter_column(_TABLE, "storage_ref", existing_type=sa.Text(), nullable=False, schema=_SCHEMA)
