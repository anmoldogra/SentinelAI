"""investigation — the ``investigation_read`` graph projection schema (ADR-0013 §1).

`service.get_case_graph` has raised ``NotImplementedError`` since Phase 8, blocked on a case→entity
bridge that no documented table provides: relationships reference evidence, cases reference
evidence,
and `database-design.md` §5 forbids the cross-schema foreign key that would join them.

The projection resolves it without touching that rule. `investigation.correlation_generated` already
carries ``case_id`` beside ``relationship_id`` (event-driven §25.8), so the *event stream* supplies
the mapping the *schema* cannot — which is the read/write asymmetry CQRS exists to exploit.

**A separate schema, in this module's own Alembic chain.** ADR-0013 §1 asks for read/write
separation; a schema boundary makes it checkable rather than aspirational. `database-design.md` §5
is
schema-per-module and the ArgoCD PreSync job applies chains in module-DAG order, so a schema named
for its content (`graph_read_models`) would belong to no module and nothing would say whose chain
owns it. Named for its owner, both answers are obvious.

**No append-only trigger and no audit columns**, unlike ADR-0004's evidentiary tables. Every row
here
is derived and disposable: dropping the schema and replaying the outbox rebuilds it exactly (§2).
Protecting a projection from mutation would protect nothing — the fact lives in
`investigation.relationships` and in the event log, both of which are already guarded.

Revision ID: 202609300010_investigation_read
Revises: 202609280007_investig_evtsig
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202609300010_investig_read"
down_revision = "202609280007_investig_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "investigation_read"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")

    op.create_table(
        "case_graph_nodes",
        # No FK to `investigation.entities`: this is a projection, and a constraint back to the
        # write side would make a rebuild fail on ordering rather than converge.
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        # Numeric and NOT NULL, mirroring `investigation.entities.confidence`. Numeric because
        # `min_confidence` is a threshold comparison and binary floating point would answer the
        # boundary case differently on each side; NOT NULL because the write side guarantees one.
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("projected_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("case_id", "entity_id", name="pk_case_graph_nodes"),
        schema=_SCHEMA,
    )
    op.create_index("ix_case_graph_nodes_case_id", "case_graph_nodes", ["case_id"], schema=_SCHEMA)

    op.create_table(
        "case_graph_edges",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rel_type", sa.String(length=100), nullable=False),
        sa.Column("from_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("projected_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("case_id", "relationship_id", name="pk_case_graph_edges"),
        schema=_SCHEMA,
    )
    op.create_index("ix_case_graph_edges_case_id", "case_graph_edges", ["case_id"], schema=_SCHEMA)
    # The traversal indexes. A recursive CTE expands one hop as "edges leaving these nodes in this
    # case", in both directions because the graph is explored undirected; without these, each hop
    # scans the case's entire edge set and `depth=3` multiplies that by three.
    op.create_index(
        "ix_case_graph_edges_case_from",
        "case_graph_edges",
        ["case_id", "from_entity_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_case_graph_edges_case_to",
        "case_graph_edges",
        ["case_id", "to_entity_id"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_case_graph_edges_case_to", table_name="case_graph_edges", schema=_SCHEMA)
    op.drop_index("ix_case_graph_edges_case_from", table_name="case_graph_edges", schema=_SCHEMA)
    op.drop_index("ix_case_graph_edges_case_id", table_name="case_graph_edges", schema=_SCHEMA)
    op.drop_table("case_graph_edges", schema=_SCHEMA)
    op.drop_index("ix_case_graph_nodes_case_id", table_name="case_graph_nodes", schema=_SCHEMA)
    op.drop_table("case_graph_nodes", schema=_SCHEMA)
    # Dropped, not left behind: the schema holds nothing but this projection, so leaving an empty
    # one would leave a downgrade that did not actually reverse itself.
    op.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA}")
