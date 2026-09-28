"""Graph projection persistence and traversal — ADR-0013 §1/§3.

Two halves, and they are deliberately the only two things that touch this schema:

* **write** — the upserts the projectors call. Every one is ``ON CONFLICT DO UPDATE``, so a
  redelivered event converges instead of colliding. That is idempotency by construction, *on top of*
  the Inbox claim the handler already performs (event-driven §17); either alone would be enough for
  the common case, and both together mean a projection cannot be corrupted by redelivery even if a
  future handler forgets the claim.
* **read** — the depth-bounded traversal that serves ``GET /cases/{case_id}/graph``, implemented as
a
  recursive CTE per ADR-0013 §3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode

# api-design.md §6: "`depth` capped at 3 to bound query cost". Enforced here as well as at the
# router's validation, because the cap is what bounds the recursion and a caller reaching this
# layer by another route must not be able to ask for an unbounded walk.
MAX_DEPTH = 3


class GraphProjectionRepository:
    """Reads and writes ``investigation_read``. Nothing else may."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- projection writes --------------------------------------------------
    async def upsert_node(
        self,
        *,
        case_id: UUID,
        entity_id: UUID,
        entity_type: str,
        canonical_name: str,
        status: str,
        confidence: Decimal,
        is_seed: bool,
        projected_at: datetime,
    ) -> None:
        """Insert or refresh one node.

        ``is_seed`` is **sticky**: once an entity has been projected as directly evidenced for a
        case, a later edge that happens to reach it as a far endpoint must not demote it. The
        ``OR`` in the conflict clause is what makes replay order irrelevant — projecting the seed
        first and the edge second, or the reverse, converges on the same row.
        """
        statement = pg_insert(CaseGraphNode).values(
            case_id=case_id,
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_name=canonical_name,
            status=status,
            confidence=confidence,
            is_seed=is_seed,
            projected_at=projected_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[CaseGraphNode.case_id, CaseGraphNode.entity_id],
                set_={
                    "entity_type": statement.excluded.entity_type,
                    "canonical_name": statement.excluded.canonical_name,
                    "status": statement.excluded.status,
                    "confidence": statement.excluded.confidence,
                    "is_seed": CaseGraphNode.is_seed.op("OR")(statement.excluded.is_seed),
                    "projected_at": statement.excluded.projected_at,
                },
            )
        )

    async def upsert_edge(
        self,
        *,
        case_id: UUID,
        relationship_id: UUID,
        rel_type: str,
        from_entity_id: UUID,
        to_entity_id: UUID,
        status: str,
        confidence: Decimal,
        projected_at: datetime,
    ) -> None:
        """Insert or refresh one edge."""
        statement = pg_insert(CaseGraphEdge).values(
            case_id=case_id,
            relationship_id=relationship_id,
            rel_type=rel_type,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            status=status,
            confidence=confidence,
            projected_at=projected_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[CaseGraphEdge.case_id, CaseGraphEdge.relationship_id],
                set_={
                    "rel_type": statement.excluded.rel_type,
                    "from_entity_id": statement.excluded.from_entity_id,
                    "to_entity_id": statement.excluded.to_entity_id,
                    "status": statement.excluded.status,
                    "confidence": statement.excluded.confidence,
                    "projected_at": statement.excluded.projected_at,
                },
            )
        )

    async def update_edge_status(
        self, *, relationship_id: UUID, status: str, projected_at: datetime
    ) -> int:
        """Set an edge's status wherever it appears; returns how many cases were touched.

        Keyed on ``relationship_id`` alone, without ``case_id``, and that is the point:
        ``investigation.finding_reviewed``'s payload does not carry a ``case_id`` (the write side
        cannot derive one — see the projector), but the projection already knows which cases the
        relationship belongs to. A review is one fact about one relationship, so it lands on every
        case graph showing it.
        """
        now_status = (
            (
                await self._session.execute(
                    select(CaseGraphEdge).where(CaseGraphEdge.relationship_id == relationship_id)
                )
            )
            .scalars()
            .all()
        )
        for edge in now_status:
            edge.status = status
            edge.projected_at = projected_at
        return len(now_status)

    async def delete_case(self, case_id: UUID) -> None:
        """Drop one case's whole projection — the unit a rebuild works in."""
        for model in (CaseGraphEdge, CaseGraphNode):
            rows = (
                (await self._session.execute(select(model).where(model.case_id == case_id)))
                .scalars()
                .all()
            )
            for row in rows:
                await self._session.delete(row)

    # -- traversal ----------------------------------------------------------
    async def read_subgraph(
        self,
        case_id: UUID,
        *,
        statuses: Sequence[str],
        entity_types: Sequence[str] | None,
        min_confidence: Decimal | None,
        depth: int,
    ) -> tuple[Sequence[CaseGraphNode], Sequence[CaseGraphEdge]]:
        """Return the filtered, depth-bounded subgraph for one case (api-design.md §6).

        **The recursive CTE, and why depth needs one at all.** ``depth`` counts "hops from
        directly-evidenced entities", so the answer is not "every node for this case" — it is the
        seed set plus whatever is reachable within *n* hops along edges that survive the filters. A
        non-recursive query cannot express reachability, and doing it in Python would mean shipping
        the case's entire edge set to the application to throw most of it away.

        **Filters are applied before traversal, not after.** An edge whose status or confidence
        excludes it is not a path — filtering afterwards would let an excluded relationship act as a
        bridge to a node the caller should not have reached, which is a subtle way to leak the
        existence of a rejected finding.

        **The traversal is undirected.** A relationship connects two entities; which one is
        ``from`` is a property of how the correlation was written, not of what an investigator is
        exploring. Expanding only forward would hide half the neighbourhood.

        Guarantees the response contract: "every relationship's endpoints are guaranteed present in
        `entities`" — edges are selected last, restricted to pairs of nodes that both survived.
        """
        depth = max(0, min(depth, MAX_DEPTH))

        node_filters: list[ColumnElement[bool]] = [
            CaseGraphNode.case_id == case_id,
            CaseGraphNode.status.in_(statuses),
        ]
        if entity_types:
            node_filters.append(CaseGraphNode.entity_type.in_(entity_types))
        if min_confidence is not None:
            node_filters.append(CaseGraphNode.confidence >= min_confidence)

        edge_filters: list[ColumnElement[bool]] = [
            CaseGraphEdge.case_id == case_id,
            CaseGraphEdge.status.in_(statuses),
        ]
        if min_confidence is not None:
            edge_filters.append(CaseGraphEdge.confidence >= min_confidence)

        # The reachable set stays **in the database**. An earlier version resolved it to a Python
        # `set[UUID]` and fed it back as an `IN (...)` bind list, which a benchmark killed outright:
        # asyncpg caps a statement at 32767 arguments, so a case with more reachable entities than
        # that failed with `InterfaceError` instead of returning a graph — and well under the cap it
        # was still shipping tens of thousands of UUIDs out and back per request. Keeping the
        # walk a subquery lets the planner join against it; nothing crosses the wire but results.
        walk = self._walk_cte(node_filters=node_filters, edge_filters=edge_filters, depth=depth)
        reachable = select(walk.c.entity_id)

        node_select = (
            select(CaseGraphNode)
            .where(*node_filters, CaseGraphNode.entity_id.in_(reachable))
            .order_by(CaseGraphNode.canonical_name.asc())
        )
        nodes = (await self._session.execute(node_select)).scalars().all()
        if not nodes:
            return [], []

        # Edges are restricted to pairs of nodes that both survived, which is what guarantees §6's
        # "every relationship's endpoints are guaranteed present in `entities`". Expressed as a
        # subquery over the same filters rather than the ids just fetched — same reason as above,
        # and it keeps the two statements consistent if one is edited without the other.
        surviving = select(CaseGraphNode.entity_id).where(
            *node_filters, CaseGraphNode.entity_id.in_(select(walk.c.entity_id))
        )
        edge_select = (
            select(CaseGraphEdge)
            .where(
                *edge_filters,
                CaseGraphEdge.from_entity_id.in_(surviving),
                CaseGraphEdge.to_entity_id.in_(surviving),
            )
            .order_by(CaseGraphEdge.relationship_id.asc())
        )
        edges = (await self._session.execute(edge_select)).scalars().all()
        return nodes, edges

    def _walk_cte(
        self,
        *,
        node_filters: Sequence[ColumnElement[bool]],
        edge_filters: Sequence[ColumnElement[bool]],
        depth: int,
    ) -> CTE:
        """The seed set expanded ``depth`` hops, as a recursive CTE (ADR-0013 §3).

        Returns the CTE rather than executing it, so callers compose it into a larger statement and
        the reachable set never leaves the database — see the note in :meth:`read_subgraph`.

        Seeds are the case's ``is_seed`` nodes that survive the node filters. A case whose seeds are
        all filtered out yields nothing — correctly: there is no hop-zero to count from.
        """
        seeds = (
            select(CaseGraphNode.entity_id.label("entity_id"), literal(0).label("hop"))
            .where(*node_filters, CaseGraphNode.is_seed.is_(True))
            .cte("graph_walk", recursive=True)
        )

        # **One** recursive term, not two. Postgres permits exactly one in a `WITH RECURSIVE`
        # (`non_recursive UNION ALL recursive`), so expanding "forward" and "backward" as separate
        # branches is invalid SQL — it fails with `InvalidRecursionError`, which is how this was
        # found. The traversal is undirected because a relationship connects two entities and
        # which one is `from` reflects how the correlation was written, not what an investigator is
        # exploring — so one step matches an edge on *either* endpoint and `CASE` returns the other.
        #
        # The CTE is referenced directly rather than through `.alias()`: aliasing it inlines its
        # definition into the non-recursive term, which is the same error by a different route.
        step_to = case(
            (CaseGraphEdge.from_entity_id == seeds.c.entity_id, CaseGraphEdge.to_entity_id),
            else_=CaseGraphEdge.from_entity_id,
        ).label("entity_id")

        # `hop < depth` is what terminates the walk. An entity graph is full of cycles, and without
        # the bound Postgres follows one until it gives up. There is deliberately no visited-set: at
        # depth ≤ 3 re-walking a node is cheaper than materializing one, and the `IN` this feeds
        # collapses the duplicates.
        step = select(step_to, (seeds.c.hop + 1).label("hop")).where(
            *edge_filters,
            seeds.c.hop < depth,
            or_(
                CaseGraphEdge.from_entity_id == seeds.c.entity_id,
                CaseGraphEdge.to_entity_id == seeds.c.entity_id,
            ),
        )

        return seeds.union_all(step)


__all__ = ["MAX_DEPTH", "GraphProjectionRepository"]
