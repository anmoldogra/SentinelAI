"""platform schema — initial (identity, sessions, roles, audit log).

Implements database-design.md §3.1. The schema itself is created by env.py before
the version table; this migration creates only the tables. ``audit_log`` is an
append-only, hash-chained ledger (§10) with a BRIN index on its time column (§6).

Revision ID: 202607210001_platform
Revises: (initial)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202607210001_platform"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "platform"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_idp_subject", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        schema=_SCHEMA,
    )
    op.create_table(
        "roles",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        sa.ForeignKeyConstraint(["user_id"], [f"{_SCHEMA}.users.user_id"]),
        sa.ForeignKeyConstraint(["role_id"], [f"{_SCHEMA}.roles.role_id"]),
        schema=_SCHEMA,
    )
    op.create_table(
        "sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], [f"{_SCHEMA}.users.user_id"]),
        schema=_SCHEMA,
    )
    op.create_table(
        "identity_provider_links",
        sa.Column("link_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idp_name", sa.Text(), nullable=False),
        sa.Column("idp_subject", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], [f"{_SCHEMA}.users.user_id"]),
        sa.UniqueConstraint("idp_name", "idp_subject", name="uq_idp_links_name_subject"),
        schema=_SCHEMA,
    )
    op.create_table(
        "audit_log",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("module", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("prev_entry_hash", sa.Text(), nullable=False),
        sa.Column("entry_hash", sa.Text(), nullable=False),
        schema=_SCHEMA,
    )
    # Append-only, time-ordered → BRIN over B-tree (database-design.md §6).
    op.create_index(
        "brin_audit_log_occurred_at",
        "audit_log",
        ["occurred_at"],
        schema=_SCHEMA,
        postgresql_using="brin",
    )


def downgrade() -> None:
    op.drop_index("brin_audit_log_occurred_at", table_name="audit_log", schema=_SCHEMA)
    op.drop_table("audit_log", schema=_SCHEMA)
    op.drop_table("identity_provider_links", schema=_SCHEMA)
    op.drop_table("sessions", schema=_SCHEMA)
    op.drop_table("user_roles", schema=_SCHEMA)
    op.drop_table("roles", schema=_SCHEMA)
    op.drop_table("users", schema=_SCHEMA)
