"""notification schema — initial (rules, notifications, deliveries + outbox + inbox).

Implements database-design.md §3.6 + outbox/inbox (consumes investigation.correlation_generated,
case.status_changed, case.report_generated — §25.9). ``source_reference_id`` is a
deliberately polymorphic app-ref paired with ``source_module`` (§5).

Revision ID: 202607210009_notification
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

revision = "202607210009_notification"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = "notification"


def upgrade() -> None:
    op.create_table(
        "notification_rules",
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trigger_event_type", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("target_role_or_user", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        schema=_SCHEMA,
    )
    op.create_table(
        "notifications",
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_module", sa.Text(), nullable=True),
        sa.Column("source_reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], [f"{_SCHEMA}.notification_rules.rule_id"]),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_notifications_recipient", "notifications", ["recipient_user_id"], schema=_SCHEMA
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.String(30), nullable=False),
        sa.Column("attempted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["notification_id"], [f"{_SCHEMA}.notifications.notification_id"]
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_deliveries_notification_id", "notification_deliveries", ["notification_id"], schema=_SCHEMA
    )
    create_outbox_events(_SCHEMA)
    create_inbox_events(_SCHEMA)


def downgrade() -> None:
    drop_inbox_events(_SCHEMA)
    drop_outbox_events(_SCHEMA)
    op.drop_index("ix_deliveries_notification_id", table_name="notification_deliveries", schema=_SCHEMA)
    op.drop_table("notification_deliveries", schema=_SCHEMA)
    op.drop_index("ix_notifications_recipient", table_name="notifications", schema=_SCHEMA)
    op.drop_table("notifications", schema=_SCHEMA)
    op.drop_table("notification_rules", schema=_SCHEMA)
