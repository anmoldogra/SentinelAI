"""threat_intel — one match row per (ioc_id, matched_evidence_id) pair.

`event-driven-architecture.md` §25.4 names that pair as the `evidence.ingested` handler's
idempotency key, and states the requirement directly: "never create a duplicate match row for the
same pair". The service checks before inserting; this constraint is what makes the guarantee hold
when that check loses a race — two workers scanning the same evidence concurrently both pass the
check, and only one insert can then succeed.

**Why a constraint and not just the check.** ADR-0011 §4's belt-and-suspenders reasoning applies,
because a duplicate match is not a cosmetic defect. Each row claims that a known-malicious indicator
appears in a specific piece of evidence, and each publishes a `threat_intel.ioc_matched` event that
`investigation` consumes — so a duplicate becomes a second correlation, a second finding for an
analyst to review, and a second line in a report about one sighting.

The index also serves `exists_for_pair`, the lookup the service performs once per candidate hit per
ingested evidence item, so the constraint costs nothing it does not already pay for.

Revision ID: 202609290011_ti_match_uq
Revises: 202609280003_threat_intel_evtsig
"""

from __future__ import annotations

from alembic import op

revision = "202609290011_ti_match_uq"
down_revision = "202609280003_threat_intel_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "threat_intel"
_TABLE = "ioc_evidence_matches"
_INDEX = "uq_ioc_evidence_match_pair"


def upgrade() -> None:
    # A unique *index* rather than a unique constraint: both enforce the same thing in Postgres,
    # and an index can later be rebuilt concurrently if this table ever grows large enough for that
    # to matter. A constraint cannot.
    op.create_index(
        _INDEX,
        _TABLE,
        ["ioc_id", "matched_evidence_id"],
        unique=True,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, schema=_SCHEMA)
