# 6. Event Dispatcher: Out-of-Process, Lock-Based, Order-Preserving

## Status

**Accepted** — implemented in modernization Wave 2.2. The relay runs in the worker
(`entrypoints/worker/main.py`), claims rows with `FOR UPDATE SKIP LOCKED`, and preserves
per-aggregate ordering (`platform/events/dispatcher.py`). See the implementation note for how §3's
ordering is achieved without the advisory lock the Decision offers as one option.

## Context

The in-process `EventDispatcher` is started in **every HTTP replica's lifespan** (verified
`entrypoints/http/main.py`) and polls with a plain `SELECT ... WHERE dispatch_status='pending'`
— **no `FOR UPDATE SKIP LOCKED`** (verified `dispatcher.py`). Consequently N HTTP replicas run
N uncoordinated pollers over the same outbox tables: duplicate delivery (masked but not
prevented by inbox dedup), wasted DB load, and no ordering guarantee — despite
`event-driven-architecture.md` §18's partition-by-`aggregate_id` intent.

## Decision

1. **Relocate the relay out of the HTTP process** into the worker (or a dedicated dispatcher
   deployment). The API process no longer runs `run_forever()`.
2. **Competing-consumers polling** with `SELECT ... FOR UPDATE SKIP LOCKED` so multiple
   dispatcher replicas partition pending rows safely and scale horizontally.
3. **Preserve per-aggregate ordering:** serialize processing per `aggregate_id` (advisory lock
   or hash-partition of schemas across dispatcher instances), so two events for the same
   aggregate never process concurrently or out of order.
4. **Backoff in the query:** gate retries on `last_attempted_at` so failed rows respect their
   policy's backoff instead of hot-looping.
5. **Transport-internal only.** The outbox write, envelope, inbox check, and event catalog are
   unchanged — this is the documented Phase-1→Phase-3 seam and the direct stepping-stone to the
   Redpanda producer/consumer.

## Implementation note (2026-09-25, Wave 2.2)

### §3's ordering comes from the claim, not from a lock

The Decision offers "advisory lock or hash-partition of schemas" for per-aggregate serialization.
Neither was needed, and avoiding them removed a moving part: the claim query takes **the oldest
pending row per `aggregate_id`** (`DISTINCT ON (aggregate_id) ... ORDER BY aggregate_id,
occurred_at`), so a batch can never hold two events for one aggregate.

Ordering across *different* dispatchers then follows from that plus the lease below. While event 1
for aggregate X is in flight it is still `pending` and still leased, so it remains the oldest
pending row for X — which means X yields nothing at all, and event 2 cannot be claimed until event 1
resolves. Strict ordering with no lock to acquire, no lock to leak, and no partition assignment to
rebalance when a replica dies.

`DISTINCT ON` lives in a subquery because Postgres rejects `SELECT DISTINCT ... FOR UPDATE` outright.

### The lease: why §2's row lock is necessary but not sufficient

`FOR UPDATE SKIP LOCKED` stops two dispatchers *selecting* the same row concurrently. It does not
stop the second one selecting it a moment later: the lock is released when the claim transaction
commits, and a row left `pending` while its handlers run is claimable again on the next poll. That
is the same double-dispatch §2 exists to prevent, arriving half a second later.

So the claim transaction also stamps `last_attempted_at`, and the claim query skips rows stamped
within `CLAIM_LEASE_SECONDS` (60s). The stamp is a lease that outlives the lock.

A `dispatching` status was the obvious alternative and is worse: a dispatcher killed mid-batch would
leave rows stranded in it, requiring a reaper process and a decision about how long is too long. An
expired lease needs neither — the row simply becomes claimable again. At-least-once is unchanged, and
post-crash redelivery is absorbed by the Inbox guard exactly as it always was.

### §4 is the same mechanism

The `last_attempted_at` gate that implements the lease *is* the retry backoff. A failed row is
re-stamped on failure, so it is not reconsidered until its window passes and retries cannot hot-loop.
One mechanism, two requirements.

### §1: one consumer list, not two

The registrar list lived in `entrypoints/http/main.py` because the dispatcher ran there. Copying it
into the worker would have been the obvious mistake — two lists drift, and the failure mode is a
handler that silently never runs in production because only the process that no longer dispatches
knew about it. It now lives once, in `entrypoints/consumers.py`, which both entrypoints import.

The worker drains the relay **before** disposing the engine, because the relay holds sessions and
tearing the pool out from under an in-flight handler would abort it mid-transaction —
event-driven-architecture.md §2.2 requires the current drain to finish instead.

### Operational consequence worth stating

With no worker running, the API still accepts writes and still writes outbox rows; nothing relays
them. Events are not lost (rows stay `pending`), but nothing downstream reacts, and the existing
`<module>_outbox_pending_count` / `_oldest_pending_age_seconds` metrics (§28) are the signal. This is
the same shape as the anchor-cutter dependency recorded in ADR-0003: worker availability is now a
correctness concern, not only a throughput one.

### Index

`(dispatch_status, aggregate_id, occurred_at)` per module schema — eight migrations rather than one,
because each module owns its schema and its own Alembic chain (database-design.md §5) and the ArgoCD
PreSync job applies those chains separately (deployment-architecture.md Part 5). The Wave 1
`(dispatch_status, occurred_at)` index is kept: it still serves the narrower "is anything pending"
question, and dropping an index from a table the relay polls under load is a separate decision.

## Consequences

- Safe horizontal scaling of both the API and the event relay; ordering preserved; the API
  process sheds background work (better tail latency).
- New operational surface: a dispatcher deployment to run, scale, and monitor.
- Migration: move startup wiring HTTP→worker; add locking + backoff to the poll; add an index
  on `(dispatch_status, aggregate_id, occurred_at)`. No event-contract change.
