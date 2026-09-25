"""platform schema — make the audit hash chain structurally a chain (ADR-0003, ADR-0004).

Appending to a hash chain is a read-modify-write with no atomicity: read the head, build an entry
naming it, insert. Two concurrent writers could read the same head and both insert, **forking** the
ledger into two branches that each verify perfectly — two contradictory histories with nothing in
the data to say which is real. ADR-0003 §1's signing widened the window, because a KMS round-trip
now sits between the read and the insert.

A ``UNIQUE`` index on ``prev_entry_hash`` closes it structurally: each entry hash may have at most
one successor, which is the difference between a chain and a tree. The second writer's insert fails
with a constraint violation and its transaction dies — fail closed, no fork. The advisory lock in
``platform.db.chain_lock`` keeps writers from racing for this constraint and wasting a signature;
**this** is what makes the outcome correct, and it holds even against a writer that bypasses the
service layer entirely.

The genesis sentinel (64 zeros) is covered by the same rule: exactly one entry may be the first.

**Not a partial index and not deferrable.** Both would create a window in which two heads exist,
which is the state this migration exists to make unrepresentable.

Adding this to a populated table fails if the ledger has already forked. That is the correct
outcome — a fork is a defect that must be investigated, not migrated past.

Revision ID: 202609080003_platform_chain
Revises: 202609080001_platform_agility
"""

from __future__ import annotations

from alembic import op

revision = "202609080003_platform_chain"
down_revision = "202609080001_platform_agility"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLE = "audit_log"
_INDEX = "uq_audit_log_prev_entry_hash"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["prev_entry_hash"], unique=True, schema=_SCHEMA)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, schema=_SCHEMA)
