"""platform schema — external anchor store for both evidentiary ledgers (ADR-0003 §3).

Signing makes *modification* detectable. It says nothing about *removal*: delete the tail of a
chain, or restore an older backup, and every remaining entry still verifies, because the evidence
of the missing entries is exactly what was removed. Closing that needs a commitment published
where the insider cannot reach — a signed Merkle root written to WORM storage.

**Why a table rather than the `anchor_ref` column ADR-0003 §5 named.** An anchor exists *after*
the entries it covers, so recording it on those rows would be an ``UPDATE`` — which ADR-0004's
append-only trigger rejects unconditionally on exactly these tables. The two ADRs contradict each
other on this point; the contradiction is resolved here in favour of ADR-0004 (never weaken
append-only) and recorded in ADR-0003. The `anchor_ref` columns stay permanently null.

**One table, both ledgers.** `ledger` is a discriminator carrying the same values the signed
message uses. There is no FK to either chain: `ingestion` is another module's schema and
database-design.md §5 forbids cross-schema FKs, so the reference is by entry hash and validated at
the application layer.

**Append-only, like the ledgers it protects.** The same ADR-0004 trigger and privilege model are
applied: an anchor a DBA can rewrite protects nothing.

Revision ID: 202609080005_platform_anchors
Revises: 202609080003_platform_chain
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sentinelai.platform.db.append_only import (
    create_reject_function_sql,
    create_trigger_sql,
    drop_trigger_sql,
)
from sentinelai.platform.db.privileges import (
    grant_evidentiary_privileges_sql,
    revoke_evidentiary_privileges_sql,
)

revision = "202609080005_platform_anchors"
down_revision = "202609080003_platform_chain"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLE = "ledger_anchors"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("anchor_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ledger", sa.Text(), nullable=False),
        sa.Column("merkle_root", sa.Text(), nullable=False),
        sa.Column("merkle_hash_algo", sa.Text(), nullable=False),
        sa.Column("first_entry_hash", sa.Text(), nullable=False),
        sa.Column("last_entry_hash", sa.Text(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.dialects.postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column("sig_alg", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("worm_object_ref", sa.Text(), nullable=False),
        sa.Column("tsa_token_ref", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )
    # One anchor per contiguous range per ledger: two commitments to the same entries would leave
    # a verifier with no way to say which is authoritative.
    op.create_index(
        "uq_ledger_anchors_ledger_range",
        _TABLE,
        ["ledger", "first_entry_hash"],
        unique=True,
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_ledger_anchors_ledger_created_at", _TABLE, ["ledger", "created_at"], schema=_SCHEMA
    )
    # ADR-0004: an anchor store that can be rewritten is not an anchor store.
    op.execute(create_reject_function_sql(_SCHEMA))
    op.execute(create_trigger_sql(_SCHEMA, _TABLE))
    op.execute(grant_evidentiary_privileges_sql(_SCHEMA, _TABLE))


def downgrade() -> None:
    op.execute(revoke_evidentiary_privileges_sql(_SCHEMA, _TABLE))
    op.execute(drop_trigger_sql(_SCHEMA, _TABLE))
    # The reject function is shared with audit_log's trigger and is NOT dropped here.
    op.drop_index("ix_ledger_anchors_ledger_created_at", table_name=_TABLE, schema=_SCHEMA)
    op.drop_index("uq_ledger_anchors_ledger_range", table_name=_TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)
