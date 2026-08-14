# 6. Event Dispatcher: Out-of-Process, Lock-Based, Order-Preserving

## Status

Proposed.

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

## Consequences

- Safe horizontal scaling of both the API and the event relay; ordering preserved; the API
  process sheds background work (better tail latency).
- New operational surface: a dispatcher deployment to run, scale, and monitor.
- Migration: move startup wiring HTTP→worker; add locking + backoff to the poll; add an index
  on `(dispatch_status, aggregate_id, occurred_at)`. No event-contract change.
