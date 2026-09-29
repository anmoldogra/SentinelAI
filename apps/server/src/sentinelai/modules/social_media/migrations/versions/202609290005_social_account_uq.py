"""social_media schema — unique ``(platform, handle)`` on ``social_accounts_observed``.

The table is a **set of accounts observed**, which is what its name and its `first_observed_at` /
`last_observed_at` columns say: `@handle` on one platform is one account however many times a
connector reports it. `database-design.md` §3.3 models the columns and no constraint, so nothing
stopped two rows describing the same account — which would split its observation window in half and
let a monitoring query miss content depending on which row it found.

`register_account` converges on the pair and refreshes `last_observed_at` instead of inserting. This
index is the half that check cannot provide: two concurrent registrations can both find nothing and
only one insert then succeeds, the same belt-and-suspenders ADR-0011 §4 asks for and the same shape
`uq_ioc_evidence_match_pair` and `uq_entity_mention_pair` already take.

Revision ID: 202609290005_social_account_uq
Revises: 202609280005_social_evtsig
"""

from __future__ import annotations

from alembic import op

revision = "202609290005_social_account_uq"
down_revision = "202609280005_social_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "social_media"
_INDEX = "uq_social_account_platform_handle"


def upgrade() -> None:
    op.create_index(
        _INDEX, "social_accounts_observed", ["platform", "handle"], unique=True, schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="social_accounts_observed", schema=_SCHEMA)
