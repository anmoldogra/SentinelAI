# 5. Transaction Boundary Ownership at the Entrypoint

## Status

**Accepted** — implemented in modernization Wave 2.1 (`platform/db/transaction.py`). See the
implementation note at the end for two deviations from the Decision as written, both deliberate.

## Context

> **Correction (Wave 2.1).** This section described 8 `self._uow.commit()` sites in
> `case_management/service.py`. By the time the ADR was implemented that was no longer true: no
> service in the codebase committed, so §2 was already satisfied and the two-independent-commits
> hazard described below could not occur. The divergence that *did* remain was different, and
> narrower — see the implementation note.

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

## Implementation note (2026-09-25, Wave 2.1)

### What was actually divergent

§2 and §3 were already satisfied when this was implemented: no service committed, and every module's
`OutboxWriter` already shared the service's session. §4's premise did not hold either —
`POST /evidence/batch` runs the whole batch in **one** transaction, and its per-item `207` results
come from pre-flush domain validation rather than per-item commits, so there were no implicit
per-item commits to remove and no savepoints to add. (A per-item *database* error still aborts the
whole batch, which is the correct outcome: it prevents a `207` body claiming success for rows that
were never committed.)

What remained was §1, half-done. The commit *was* at the entrypoint, but it was hand-written in each
handler — twenty-four `await uow.commit()` calls — and rollback was implicit, relying on
`AsyncSession.close()` discarding an uncommitted transaction rather than being stated anywhere.

**That shape is a silent-data-loss hazard.** A handler that omits the call does not fail, log, or
raise: the session closes, the transaction is discarded, and the endpoint returns `201` describing a
row that does not exist. Nothing notices unless a test happens to assert persistence. The twenty-four
calls are now gone; the boundary owns the commit, and a new handler cannot forget it.

### Deviation 1: a route class, not the `yield` dependency §1 suggests

§1 says "an HTTP dependency … does `async with uow:` … committing once on success". A `yield`
dependency cannot do this safely, and its failure mode is the worst kind. **FastAPI runs dependency
teardown after the response has been produced**, so an exception from `commit()` there cannot become
a `500`: it escapes with the response already begun, bypassing the registered exception handlers and
the standard error envelope. The client is told the write succeeded when the transaction never
committed. This was verified empirically before choosing the design, not assumed.

The boundary is therefore a custom `APIRoute` (`TransactionalRoute`), whose wrapper runs *inside* the
request/response cycle, so a failing commit still reaches the exception handlers. A router-level
`Depends(bind_session)` publishes the request-scoped session for it — declared on the router rather
than per-handler so no handler can omit it, and resolved through `get_session` so dependency
overrides in tests keep working.

`test_a_failing_commit_does_not_report_success` pins the property this deviation exists for.

### Deviation 2: three endpoints still commit explicitly, and must

Some failures are themselves auditable facts whose writes have to survive the error response:

| Endpoint | What must persist through the error | Authority |
|---|---|---|
| `POST /evidence` (422) | intake record + `evidence.validation_failed` event | event-driven §25.2 |
| `POST /evidence/{id}/verify-integrity` (409) | MISMATCH custody entry + audit row | ADR-0008 §3 |
| `POST /auth/login` (401) | `login_failed` audit row | security-architecture §5 |

Each commits and then re-raises. This composes with the boundary without either side knowing about
the other: the rollback the boundary performs on the way out finds an already-committed transaction
and does nothing. Expressed this way rather than as an exception-class allowlist, because "which
writes survive which failure" is a decision belonging to the endpoint that made them, not a global
property of an HTTP status code.

### Scope

The worker job wrappers and the event dispatcher already owned their transactions in the shape §1
requires (try / commit / except / rollback) and were left unchanged. The health and metrics probes
carry no boundary deliberately — they own no business writes — and a test asserts that the set of
unguarded routes is exactly those four, so the exclusion reads as a decision rather than an
oversight.
