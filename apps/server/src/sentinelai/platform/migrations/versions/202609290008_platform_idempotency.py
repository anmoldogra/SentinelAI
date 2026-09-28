"""platform schema — ``idempotency_keys``, the API idempotency store (ADR-0012 §1).

`api-design.md` §2.9 has specified an ``Idempotency-Key`` header since the API was designed, and
twenty endpoints are marked "Yes (key)" — with no store behind any of them. A client retry after a
timeout therefore double-created resources, evidence included, which PRD FR-1.3 and the whole
chain-of-custody argument cannot tolerate.

**The unique constraint is the concurrency control**, not just an integrity rule. Two simultaneous
requests carrying one key race to insert here, and Postgres makes the loser wait on the index entry
until the winner's transaction ends — ADR-0012 §2(d)'s "serialize (row lock)" without any explicit
locking, and better than its ``409`` alternative for a client whose only mistake was retrying.

**No append-only trigger on this table** (unlike ADR-0004's evidentiary tables), deliberately. A row
is written twice by design — claimed, then completed — and deleted by the TTL sweep. It records what
an API call answered, not what happened to evidence; the append-only record of that is
``platform.audit_log``, which this table does not duplicate and must not be confused with.

Revision ID: 202609290008_platform_idem
Revises: 202609080005_platform_anchors
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202609290008_platform_idem"
down_revision = "202609080005_platform_anchors"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLE = "idempotency_keys"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("idempotency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # app-ref to platform.users. No FK even though this IS the platform schema: a key must
        # survive the deletion of the principal that created it for the rest of its window, and a
        # cascade here would silently un-deduplicate a retry mid-flight.
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="claimed"),
        # All three NULL while `state = 'claimed'`, set together on completion.
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(), nullable=True),
        # bytea: §2.9 requires a byte-identical replay, and decoding to text would corrupt the
        # first response body that is not valid UTF-8.
        sa.Column("response_body", postgresql.BYTEA(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("idempotency_id", name="pk_idempotency_keys"),
        sa.UniqueConstraint("principal_id", "idempotency_key", "path", name="uq_idempotency_claim"),
        schema=_SCHEMA,
    )
    # The TTL sweep's index (§3). Not partial on `state`: the sweep deletes by age regardless of
    # state, and a partial index would exclude exactly the abandoned rows worth reclaiming.
    op.create_index("ix_idempotency_keys_expires_at", _TABLE, ["expires_at"], schema=_SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name=_TABLE, schema=_SCHEMA)
    op.drop_table(_TABLE, schema=_SCHEMA)
