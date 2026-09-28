"""case_management schema — ``case_members``, the ABAC case-scope grant (ADR-0017 §1).

``security-architecture.md`` §6 evaluates "case-scope grant" as the first ABAC attribute, and
there was nothing to evaluate it against: ``cases.owning_user_id`` made a case reachable by
exactly one person, so §6's own worked example described a check that could not be written.
``database-design.md`` §3.4 is updated in the same change as this migration.

The composite primary key ``(case_id, user_id)`` is load-bearing rather than incidental — a user
is a member of a case once, with one role, and that is what makes
``PUT /cases/{case_id}/members/{user_id}`` naturally idempotent instead of needing an idempotency
key: a re-grant updates the existing row rather than inserting a second, conflicting membership.

``case_id`` is a real intra-schema foreign key with ``ON DELETE CASCADE``; ``user_id`` and
``granted_by_user_id`` are unenforced app-refs to ``platform.users``, because ``database-design``
§5 forbids cross-schema foreign keys. The cascade is safe here in a way it would not be on an
evidentiary table: a membership grant is access-control state, not a record of fact, and the
audit-log entry for the grant survives independently of the row.

No index beyond the primary key. ``(case_id, user_id)`` serves the access check (both values are
known), and the PK's leading ``case_id`` serves the member listing. A reverse index on
``user_id`` would serve "every case this user is on", which no endpoint asks for — the case list
is already scoped by a query this migration does not change.

Revision ID: 202609290007_case_members
Revises: 202609280006_case_mgmt_evtsig
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202609290007_case_members"
down_revision = "202609280006_case_mgmt_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "case_management"


def upgrade() -> None:
    op.create_table(
        "case_members",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        # app-ref to platform.users — no cross-schema FK (database-design.md §5).
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("granted_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        # `ForeignKeyConstraint` rather than an inline `ForeignKey`, matching this module's other
        # migrations. The name follows `Base`'s convention, so the constraint Alembic creates and
        # the one `create_all` creates in tests carry the same name.
        sa.ForeignKeyConstraint(
            ["case_id"],
            [f"{_SCHEMA}.cases.case_id"],
            name="fk_case_members_case_id_cases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("case_id", "user_id", name="pk_case_members"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("case_members", schema=_SCHEMA)
