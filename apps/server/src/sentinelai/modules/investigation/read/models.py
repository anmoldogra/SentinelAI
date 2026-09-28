"""Graph read-model ORM — schema ``investigation_read`` (ADR-0013 §1).

A **projection**, not a source of truth. Every row here is derived from integration events and is
disposable: dropping the schema and replaying the outbox rebuilds it exactly (ADR-0013 §2). Nothing
writes here except the projectors, and nothing reads the transactional tables to serve a graph.

**Why a separate schema rather than two more tables in ``investigation``.** ADR-0013 §1 asks for
read/write separation, and a schema boundary is what makes that checkable instead of aspirational: a
query against `investigation_read` provably touches no transactional row, the projection can be
truncated and rebuilt without a migration against live case data, and a future move to a read
replica is a connection-string change rather than a table-by-table audit.

**Why `investigation_read` and not `graph_read_models`.** `database-design.md` §5 is
schema-per-module, and the ArgoCD PreSync job applies migrations in module-DAG order — a schema
named for its *content* would belong to no module, so nothing would say whose Alembic chain owns it
or when it gets applied. Naming it for its owner keeps both answers obvious: `investigation` builds
it, `investigation`'s chain migrates it, and the `_read` suffix says it is the read half.

**The case→entity bridge this makes possible.** `service.get_case_graph` was deferred because no
documented table maps a case to its entities: relationships reference evidence, cases reference
evidence, and `database-design.md` §5 forbids the cross-schema join that would connect them. The
projection sidesteps that entirely — `investigation.correlation_generated` already carries `case_id`
alongside `relationship_id` (event-driven §25.8), so the *event* supplies the mapping the *schema*
cannot, which is precisely the read/write asymmetry CQRS exists to exploit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

SCHEMA = "investigation_read"


class CaseGraphNode(Base):
    """One entity as it appears in one case's graph.

    Denormalized on purpose: `entity_type`, `canonical_name`, `status` and `confidence` are copied
    from the transactional row so a graph read is one indexed scan of this table and never a join
    back to `investigation.entities`. That copy is the cost of CQRS and the reason the projection is
    rebuildable — it holds no fact the write side does not already own.

    The same entity appears once **per case** (`(case_id, entity_id)` is the key), because an entity
    can be relevant to several cases and each case's graph is served independently. A single global
    node row would make one case's filter leak into another's read.
    """

    __tablename__ = "case_graph_nodes"
    __table_args__ = (
        # The graph read's access path: every query starts "all nodes for this case", then filters.
        Index("ix_case_graph_nodes_case_id", "case_id"),
        {"schema": SCHEMA},
    )

    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    # `Numeric` and NOT NULL, mirroring `investigation.entities.confidence` exactly. Numeric
    # because a confidence is compared against a threshold (`min_confidence`) and binary floating
    # point would answer the boundary case differently here than on the write side (ADR-0011 §2);
    # NOT NULL because the write side guarantees one, and a nullable projection column would be
    # inventing a state the source cannot produce — along with a filter branch to handle it.
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    # Whether this entity was produced *for* this case, rather than pulled in as the far endpoint of
    # a relationship. `depth` in api-design.md §6 counts "hops from directly-evidenced entities", so
    # traversal needs to know where hop zero is; without this flag every node would be its own seed
    # and `depth` would mean nothing.
    is_seed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When the projection last wrote this row. Not the entity's own timestamp — it is the staleness
    # signal ADR-0013 §2 calls for ("bounded, monitored"), and the only honest answer to "how old is
    # this graph?".
    projected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class CaseGraphEdge(Base):
    """One relationship as it appears in one case's graph.

    Keyed `(case_id, relationship_id)` for the same reason nodes are: the projection answers
    per-case reads, and a relationship generated for two cases is two edges with independent
    lifecycles.

    Endpoints are plain UUIDs with no foreign key to :class:`CaseGraphNode`, even though both live
    in this schema. A projector writes an edge and its endpoints in one transaction, so the
    constraint would hold — but it would also make the order of those writes load-bearing, and a
    rebuild that replayed events in a different order would fail on a constraint rather than
    converge. The read query joins on these columns and a missing endpoint simply drops the edge,
    which is the right behaviour for a disposable view.
    """

    __tablename__ = "case_graph_edges"
    __table_args__ = (
        Index("ix_case_graph_edges_case_id", "case_id"),
        # The traversal index: a recursive CTE expands by "edges leaving these nodes in this case",
        # and without this each hop degrades into a scan of the case's whole edge set.
        Index("ix_case_graph_edges_case_from", "case_id", "from_entity_id"),
        Index("ix_case_graph_edges_case_to", "case_id", "to_entity_id"),
        {"schema": SCHEMA},
    )

    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    relationship_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    rel_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    projected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


__all__ = ["SCHEMA", "CaseGraphEdge", "CaseGraphNode"]
