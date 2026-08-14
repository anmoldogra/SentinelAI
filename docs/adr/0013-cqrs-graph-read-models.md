# 13. CQRS & Graph Read Models

## Status

Proposed (Phase 2). Depends on ADR-0006 (reliable event delivery to build projections).

## Context

The entity/relationship graph and cross-domain correlation are read over **normalized
Postgres** tables; `get_case_graph` is deferred. At the target scale (10M+ relationships,
thousands of concurrent investigators, depth-bounded neighborhood queries, the frontend's
server-filtered subgraph requirement), k-hop traversal and correlation over normalized tables
will not meet interactive latency, and there is no read/write separation.

## Decision

1. **Separate read and write models** for the heavy read paths. The write side stays normalized
   (source of truth, unchanged). Build **read projections** updated from integration events:
   case subgraph, depth-bounded entity neighborhood, the `status=proposed` review queue, and
   case/finding statistics.
2. **Projections are disposable and rebuildable** from the event log — no second source of
   truth. Bounded staleness (seconds) is acceptable for analytical/review reads and is already
   compatible with the human-in-the-loop model.
3. **Traversal strategy is benchmark-gated:** start with recursive CTEs over a
   traversal-optimized projection; adopt a dedicated **graph datastore (e.g. Apache AGE / Neo4j)
   as a read replica** only if measured CTE latency at target cardinality is insufficient — a
   new datastore is a decision that requires evidence, per ADR-0001 discipline. This ADR does
   **not** pre-commit to a graph DB.
4. Reads never compute a filtered subgraph client-side; the projection serves the server's
   filtered view (frontend-architecture §Graph).

## Consequences

- Interactive graph/correlation reads at scale; write path unaffected.
- Eventual consistency of read models (bounded, monitored); more infrastructure (projection
  builders + rebuild jobs); a possible future graph datastore gated on benchmarks.
- This is a Phase-2 concern — it does not block Beta.
