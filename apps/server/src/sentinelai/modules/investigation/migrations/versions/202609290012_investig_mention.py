"""investigation schema — unique ``(entity_id, evidence_id)`` on ``entity_evidence_mentions``.

`event-driven-architecture.md` §12 requires every consumer to carry a **business** natural key
alongside the Inbox check, and §25.8 names this handler's: `(ioc_id, matched_evidence_id)` — "the
match as correlation input for the case owning the matched evidence". `investigation` records that
match as a CEM §11 MENTIONS edge from the matched evidence to the indicator entity, so on this side
the same key *is* `(entity_id, evidence_id)`.

The handler checks for the pair before inserting. This index is the half the check cannot provide:
two dispatchers can both pass the check and only one insert then succeeds. It is worth a migration
because the row is not bookkeeping — a MENTIONS edge is what grounds an entity's existence under
CEM §13, and a duplicate would double-count the evidence supporting a finding an analyst reviews.

A unique index is also simply *true* of the table as `database-design.md` §3.5 models it: the
columns are `(mention_id, entity_id, evidence_id)` with nothing to distinguish two rows for one pair
— no offset, no span, no occurrence count. "This evidence mentions this entity" is set membership,
not a countable event.

Revision ID: 202609290012_investig_mention
Revises: 202609300010_investig_read
"""

from __future__ import annotations

from alembic import op

revision = "202609290012_investig_mention"
down_revision = "202609300010_investig_read"
branch_labels = None
depends_on = None

_SCHEMA = "investigation"
_INDEX = "uq_entity_mention_pair"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "entity_evidence_mentions",
        ["entity_id", "evidence_id"],
        unique=True,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="entity_evidence_mentions", schema=_SCHEMA)
