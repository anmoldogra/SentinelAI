"""ingestion schema — make each evidence item's custody chain structurally a chain (ADR-0003).

The custody half of ``202609080003_platform_chain``; see that migration for the full reasoning on
the fork race and why a unique constraint rather than a lock is what makes the outcome correct.

Two constraints, because the custody ledger carries two independent orderings and a fork would
violate either one:

* ``(evidence_id, prev_event_hash)`` — the hash link. One successor per entry, per evidence item.
  Scoped to ``evidence_id`` rather than global because every chain starts from the same all-zero
  genesis sentinel, so a global unique index would permit exactly one evidence item to exist.
* ``(evidence_id, sequence_number)`` — the declared ordering. CEM §4 calls this "monotonically
  increasing per evidence_id"; until now nothing enforced it, so two entries could share a
  sequence number and a reader ordering by it would see an arbitrary one.

Both are needed. The hash link alone would allow a duplicated sequence number on a well-formed
chain; the sequence alone would allow two entries at different positions to name the same
predecessor.

Revision ID: 202609080004_ingestion_chain
Revises: 202609080002_ingestion_agility
"""

from __future__ import annotations

from alembic import op

revision = "202609080004_ingestion_chain"
down_revision = "202609080002_ingestion_agility"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"
_TABLE = "evidence_custody_events"
_LINK_INDEX = "uq_custody_events_evidence_prev_hash"
_SEQUENCE_INDEX = "uq_custody_events_evidence_sequence"


def upgrade() -> None:
    op.create_index(
        _LINK_INDEX, _TABLE, ["evidence_id", "prev_event_hash"], unique=True, schema=_SCHEMA
    )
    op.create_index(
        _SEQUENCE_INDEX, _TABLE, ["evidence_id", "sequence_number"], unique=True, schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_index(_SEQUENCE_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_index(_LINK_INDEX, table_name=_TABLE, schema=_SCHEMA)
