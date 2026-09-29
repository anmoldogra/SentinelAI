# 13. CQRS & Graph Read Models

## Status

**Accepted — Built for the case graph** in modernization Wave 4.1. Depends on ADR-0006 (reliable
event delivery to build projections), in place since Wave 2.2.

| Decision | State |
|---|---|
| §1 Separate read model, updated from integration events | **Built** — `investigation_read` schema, `case_graph_nodes` / `case_graph_edges` |
| §1 Case subgraph projection | **Built** — and it closes `get_case_graph`, deferred since Phase 8 |
| §1 Depth-bounded entity neighbourhood | **Built** as the traversal; still **inert in practice** — see "what bounds `depth`" |
| §1 `status=proposed` review queue projection | **Not built** — the existing `list_relationships(status=...)` query serves it from the write side and is not a measured bottleneck |
| §1 Case/finding statistics projection | **Not built** — no endpoint consumes one |
| §2 Projections disposable and rebuildable from the event log | **Built** — proven by a drop-and-replay test, not asserted |
| §3 Traversal strategy benchmark-gated | **Resolved: native PostgreSQL recursive CTE. No graph datastore.** See the decision below |
| §4 Server-side filtered subgraph, never client-side | **Built** — `status`, `entity_types`, `min_confidence`, `depth` all applied in SQL |

## §3 resolved: recursive CTEs over PostgreSQL, and no graph datastore

§3 said a dedicated graph store would be adopted "only if measured CTE latency at target cardinality
is insufficient — a new datastore is a decision that requires evidence". Here is the evidence.

Measured on the built projection against PostgreSQL 16, one case, five samples per point, p50:

| Entities | Edges | depth=1 | depth=2 | depth=3 | Returned at depth=3 |
|---|---|---|---|---|---|
| 1,000 | 3,000 | 15 ms | 40 ms | 75 ms | 993 nodes / 2,991 edges |
| 10,000 | 30,000 | 153 ms | 705 ms | 1,417 ms | 9,941 nodes / 29,942 edges |
| 50,000 | 200,000 | 1,793 ms | 6,612 ms | 13,996 ms | 49,975 nodes / 199,990 edges |

**Read the fourth column before the third.** These graphs are single dense components, so a 3-hop walk
reaches essentially every node — the query is returning 50,000 nodes and 200,000 edges in one
unpaginated response. The 14 seconds is dominated by materializing and serializing that result, not by
traversing to find it. A graph database would not make it faster; it would return the same 250,000
elements just as slowly.

So the measurement does not say "CTEs are too slow". It says **`depth` over a dense component is the
wrong query to answer**, and the limit is response size. `api-design.md` §6 already assumes it is not
one ("a case's subgraph is not expected to be unbounded the way `evidence` lists are") and offers no
pagination. The mitigation is therefore a **node cap** on the response, not a new storage engine — and
that is a change to this API, cheap, reversible, and recorded below as not yet built.

**The decision: stay on PostgreSQL.** Beyond the numbers, three reasons compound, and the first is
decisive on its own:

1. **Air-gapped and classified deployments (`deployment-architecture.md`'s zero-egress profiles).**
   A second datastore means a second image to mirror into the enclave, a second backup and restore
   path, a second set of credentials in Vault, a second thing to patch on an airgapped update cycle,
   and a second failure mode for an operator with no internet to search from. Apache AGE is a
   Postgres extension and would avoid the separate *service* — but it is still a component that must
   be present in the enclave's image and version-matched to the server, for a query shape that is not
   the bottleneck.
2. **The projection is already the optimization.** The expensive thing about the pre-CQRS design was
   not traversal — it was that no case→entity mapping existed at all, so the query could not be
   written. Denormalizing the case's subgraph into two indexed tables is what made it a bounded,
   index-served read. A graph engine would be optimizing the step that is now cheap.
3. **Rebuildability constrains the choice.** §2 requires projections be disposable and rebuildable
   from the event log. A second datastore means rebuild is a cross-system operation that can half-fail
   — the projection is consistent in Postgres and stale in the graph store, with no transaction
   spanning both. Inside one database, a rebuild is one transaction per case.

This resolves the roadmap's "benchmark-gated graph-store decision" as **no adoption**, and the gate
stays open: if a real deployment shows a *bounded* subgraph (a few thousand nodes) missing interactive
latency, that is new evidence and this decision should be revisited. The numbers above are not that
evidence.

## Implementation note (2026-09-29, Wave 4.1)

### The projection closes a deferral that had nothing to do with performance

`service.get_case_graph` raised `NotImplementedError` for eight phases, and the reason was structural,
not slow: **no documented table maps a case to its entities.** Relationships reference evidence, cases
reference evidence, and `database-design.md` §5 forbids the cross-schema foreign key that would join
them.

`investigation.correlation_generated` already carries `case_id` beside `relationship_id`
(`event-driven-architecture.md` §25.8). So the **event stream supplies the mapping the schema cannot**
— which is the read/write asymmetry CQRS exists to exploit, and not a loophole in §5: nothing joins
across a schema at query time, because the join already happened when the event was published.

### A separate schema, named for its owner

`investigation_read`, not `graph_read_models`. §1 asks for read/write separation and a schema boundary
makes it checkable rather than aspirational — a query against `investigation_read` provably touches no
transactional row. But `database-design.md` §5 is schema-per-module and §11 orders migrations by
module, so a schema named for its *content* would belong to no module: nothing would say whose Alembic
chain owns it or when the ArgoCD PreSync job applies it. Named for its owner, both answers are
obvious, and the `_read` suffix carries the rest.

### Idempotency is doubled, deliberately

Every projector performs the Inbox claim before any side effect (§17), **and** every projection write
is an `ON CONFLICT DO UPDATE` that converges. Either alone handles ordinary redelivery. Together they
mean the projection survives a replay that deliberately clears the inbox — which §Replay describes as
a normal operation — and a future handler that forgets the claim. `is_seed` is folded with `OR` rather
than overwritten, so replay *order* cannot demote a seed and silently change what `depth` returns.

### What bounds `depth` today

§6 defines `depth` as "hops from directly-evidenced entities". A relationship announced by
`correlation_generated` was generated for a case from evidence linked to it, so both endpoints are
directly evidenced: hop zero.

**Updated (the threat-intel loop).** §25.8's `entity_id` variant of `correlation_generated` is now
produced — `investigation`'s `threat_intel.ioc_matched` consumer publishes it for each matched
indicator, and `evidence.linked_to_case` projects what an evidence item already grounds into a case
it is linked to afterwards. So the projection no longer holds only correlation-job output, and the
entity-level projection this section said §25.8 did not define turned out to be the variant §25.8
already specified.

**`depth` still does not discriminate, and saying otherwise would be wrong.** Every node the
projection holds is still a seed: a matched indicator is mentioned by evidence linked to the case, and
so is every entity it is associated with, so all of them are hop zero. A read at depth 1, 2 or 3
returns the same subgraph — the case's own findings, which is exactly what §6's default (`depth=1`)
should return.

What would change that is an edge reaching an entity *not* grounded in the case's evidence — threat
actor attribution is the obvious one: the actor a matched indicator belongs to is a real entity one
hop out, and `threat_intel.iocs.threat_actor_id` already records the attribution. It is not built
because the relationship would have no honest `supporting_evidence_ids`: the case's evidence shows the
indicator, not the attribution, whose provenance is the feed — and CEM §13 rejects a relationship
without ≥1 supporting evidence. Closing it needs the IOC published into the CEM as its own evidence
object (`iocs.evidence_id`, §3.3, still null), which is a separate increment. The traversal remains
built ahead of that because it is §3's decision and because it makes `depth` correct on the day those
edges arrive rather than a migration away from it.

### Two bugs the work surfaced

**The reachable set must not leave the database.** The first implementation resolved the walk to a
Python `set[UUID]` and fed it back as an `IN (...)` bind list. The benchmark killed it: asyncpg caps a
statement at 32,767 arguments, so a case with more reachable entities than that failed with
`InterfaceError` instead of returning a graph — and well under the cap it was shipping tens of
thousands of UUIDs out and back per request. The walk is now a subquery the planner joins against.

**A `Decimal` query parameter returned 500 instead of 400.** `min_confidence` is the first
non-JSON-native query parameter in the codebase, and the `RequestValidationError` handler put
`exc.errors()` — which echoes the offending *input*, coerced to `Decimal` — straight into a
`JSONResponse`. `json.dumps` raised inside the handler, the unhandled-exception handler caught it, and
an out-of-range value came back as a server error. Fixed with `jsonable_encoder`, and pinned by
`tests/unit/test_error_envelope.py`.

### Known staleness beyond dispatcher latency

`review_entity_status` publishes **nothing** — it writes a revision row and an audit entry, and says
so in a comment ("audit only") — and §25.8 defines no entity-disposition event. So a projected node's
`status` refreshes only when one of its relationships is re-projected. Pinned by a test so it is a
property rather than a surprise; closing it needs an event this ADR will not invent.

### Not built, and deliberately

* **A response node cap.** The benchmark says this, not a graph store, is the mitigation for a dense
  subgraph. It is an `api-design.md` §6 change (a documented cap plus a truncation signal), so it
  belongs with that edit rather than smuggled in here.
* **The review-queue and statistics projections** (§1). The existing write-side queries serve both and
  neither is a measured bottleneck; projecting them now would add two more things to keep consistent
  for no evidence.
* **A scheduled rebuild job.** `delete_case` plus replay is the mechanism and is tested; wiring it to
  a schedule needs an operator-facing trigger and a decision about what a rebuild does to a case being
  actively read, which is an operations concern rather than a projection one.

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
