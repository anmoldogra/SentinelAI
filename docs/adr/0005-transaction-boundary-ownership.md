# 5. Transaction Boundary Ownership at the Entrypoint

## Status

Proposed.

## Context

Service methods currently call `self._uow.commit()` themselves (verified: 8 sites in
`case_management/service.py` alone; `get_session` never commits). Any workflow that composes
two service methods therefore produces **two independent commits**, not one atomic
transaction — a correctness hazard as cross-module orchestration grows (the roadmap requires
composite flows: ingest→link→correlate→notify). This is transaction/aggregate leakage: the
transaction boundary is an application-flow concern, not a service concern.

## Decision

1. **The UnitOfWork is opened and committed at the entrypoint boundary**, not in services:
   an HTTP dependency (and the worker job wrapper) does `async with uow: <call service(s)>`,
   committing once on success and rolling back on any exception.
2. **Services never `commit()`/`rollback()`.** They mutate through the injected UoW and raise
   on failure. Composed service-to-service calls run inside the single ambient transaction.
3. **Outbox writes stay inside that same transaction** (unchanged — preserves §16 atomicity).
4. **Independent-per-item semantics are explicit.** Where a batch intentionally commits per
   item (e.g. `POST /evidence/batch`), the loop opens an explicit nested
   transaction/savepoint per item — implicit per-item commits are removed.

## Consequences

- True atomicity for multi-step, multi-module workflows; no partial writes across services.
- Service code simplifies (no commit bookkeeping) and becomes trivially composable.
- Requires a small UoW-lifecycle change in the DI layer and the worker wrapper; internal
  (non-API-breaking) refactor of every implemented service to remove commits.
- Batch endpoints must adopt explicit savepoint handling to keep their documented per-item
  result semantics.
