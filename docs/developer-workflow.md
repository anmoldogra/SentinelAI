# SentinelAI — Developer Workflow

**Status:** Mandatory operational workflow. Every contributor — human or AI coding assistant —
follows it for day-to-day development.

**Where this sits.** `engineering-standards.md` defines **how code must look**; this document
defines **how development happens**. It is not a Git tutorial, not architecture, not project
planning — it is the executable, repeatable workflow of working inside SentinelAI.

**Frozen sources (never contradicted, never extended here):** `engineering-governance.md`
(precedence winner on any conflict), the approved ADRs, `architecture.md`, `system-design.md`,
`backend-implementation-guide.md` (the *how* authority, cited "guide PartN"), `security-architecture.md`,
`database-design.md`, `event-driven-architecture.md`, `api-design.md`, `canonical-evidence-model.md`,
`deployment-architecture.md`, `engineering-standards.md`, `implementation-wave-1.md`. **Precedence on
conflict:** governance → ADRs → architecture → implementation manual. This document introduces no new
architecture, ADR, technology, pattern, or repository structure — it only sequences the work the
frozen docs already define.

**Reading each section:** Purpose · Rules · Workflow · Examples · Anti-patterns · Common mistakes ·
Review checklist. Commands are real (`apps/server/Makefile`); examples are illustrative only.

---

## 1. Local development setup

**Purpose.** A working local environment that mirrors production topology (system-design §13) so
"works on my machine" means "works in the pipeline."

**Rules.**
- **Prerequisites (workflow tools, not runtime deps):** Python **3.12+**; **uv** (recommended
  virtual-env / dependency manager — it operates on the existing `pyproject.toml` and changes no
  declared dependency); **Docker + Docker Compose**; `make`. No other global tooling is required or
  permitted for the core flow.
- Runtime dependencies are **only** those declared in `apps/server/pyproject.toml` (fixed by
  ADR-0002). MUST NOT `pip install` anything not declared there; adding a dependency is an ADR-gated
  decision (§5), never a local convenience.
- **Local profile is `development`** with `KMS_PROVIDER=dev` (software keystore) — the dev stack does
  **not** require Vault. The `dev` KMS provider is **forbidden in production** (fails closed).
- Secrets never leave the machine: `.env` is dev-only and git-ignored; copy from `.env.example`,
  which holds placeholders only. Never commit `.env`.
- **Testcontainers** provides real Postgres/Redis/MinIO for integration tests; they require Docker
  and are skipped (never falsely passed) when Docker is unavailable.

**Workflow.**
```bash
cd apps/server
uv venv && source .venv/bin/activate      # (or: python -m venv .venv). uv is the recommended manager.
make install                              # pip install -e ".[dev]" — installs the declared stack + dev extras
cp .env.example .env                       # dev placeholders; edit nothing secret
make compose-up                           # postgres + redis + api + worker (dev stack)
make migrate                               # apply per-module migrations in DAG order (scripts/migrate.sh)
make run-api                               # uvicorn …http.main:app --reload   (in one shell)
make run-worker                            # arq …worker.main.WorkerSettings    (in another)
curl -fsS localhost:8000/healthz           # {"status":"ok"}
curl -fsS localhost:8000/readyz            # 200 only when postgres+redis+kms reachable
```

**Environment variables & profiles.** Field names in `platform/config.py` map to `UPPER_SNAKE` env
vars; the profile is `APP_ENV`. Local dev uses `development`; the five profiles
(`development`/`testing`/`production`/`air-gapped`/`classified`) and their fail-closed rules are
defined in `implementation-wave-1.md` §5 — do not invent a sixth.

**Local services.** The current dev stack is **postgres, redis, api, worker** (`docker-compose.dev.yml`).
MinIO (object storage) and a Vault-dev container are added by Wave-1 task W1-14; until then object-
storage work uses the in-memory fake adapter and KMS uses the `dev` provider. Do not point local dev
at a shared/remote database or a real Vault.

**Examples.**
```bash
make check        # run every gate locally exactly as CI does (lint typecheck lint-imports test)
make compose-down # tear the stack down
```

**Anti-patterns.** Installing an undeclared package; running against a shared DB; committing `.env`;
using the `dev` KMS provider anywhere but locally; hand-editing production-shaped secrets locally.

**Common mistakes.** Forgetting `make migrate` after `compose-up`; running the API without the worker
(events won't dispatch in the split-process model); Docker not running so integration tests silently
skip and a gap is missed.

**Review checklist.**
- [ ] Only declared dependencies installed; `.env` from `.env.example`, not committed.
- [ ] `development` profile + `dev` KMS locally; not pointing at shared infra.
- [ ] `make check` passes before pushing.

---

## 2. Repository bootstrap

**Purpose.** Get from a fresh clone to a green `make check` deterministically.

**Rules.**
- Bootstrap MUST be reproducible from the repo alone (no undocumented steps).
- The first thing a new contributor reads is `CLAUDE.md` + `engineering-governance.md` +
  `engineering-standards.md` — code before reading those is a process defect.

**Workflow.**
```bash
git clone <repo> && cd SentinelAI/apps/server
uv venv && source .venv/bin/activate
make install
cp .env.example .env
make compose-up && make migrate
make check                                 # must be green on a clean checkout
```

**Examples.** `make help` lists every available target (self-documenting Makefile).

**Anti-patterns.** A bootstrap that needs a Slack message to complete; skipping `make check` on first
clone and assuming green.

**Common mistakes.** Wrong Python version (must be 3.12+); stale `.venv` from another project;
forgetting to activate the venv so `make` uses the system interpreter.

**Review checklist.**
- [ ] Clean clone reaches green `make check` with only documented steps.
- [ ] New contributor has read governance + standards before first PR.

---

## 3. Daily development workflow

**Purpose.** A tight, gate-first loop that catches violations before they reach CI.

**Rules.**
- Work on a short-lived branch off `main` (§4); never on `main`.
- Run the relevant gate continuously; **`make check` MUST be green before every push** (it is the
  local mirror of CI: `lint typecheck lint-imports test`).
- Keep changes small and single-purpose (one use case / one endpoint / one event per PR).
- Update the authoritative doc/ADR/event catalog **in the same change** as the code (governance §2).

**Workflow.**
1. `git switch -c feat/<slug>` from an up-to-date `main`.
2. Read the frozen docs for the area (§6–§12 point to the right ones).
3. Write the failing test first (TDD is the default for domain logic).
4. Implement to the standards (`engineering-standards.md`).
5. `make format && make lint && make typecheck && make lint-imports && make test` (or just
   `make check`).
6. Update docs/catalog; self-review against §22.
7. Commit (Conventional Commits), open a focused PR (§4, §22).

**Examples.**
```bash
make test PYTEST_ARGS="-k close_case"     # fast focused loop (if wired) — else: pytest -k close_case
make lint-imports                          # confirm no boundary was crossed
```

**Anti-patterns.** Batching five unrelated changes into one PR; pushing red locally "to let CI check";
skipping the doc update "for a follow-up."

**Common mistakes.** Forgetting `make lint-imports` (boundary regressions surface only there);
editing generated/frozen contracts as a side effect; leaving a `TODO` in code presented as done.

**Review checklist.**
- [ ] Branch is short-lived and single-purpose.
- [ ] `make check` green locally; docs/catalog updated in the same change.

---

## 4. Branch workflow

**Purpose.** A clean, revertible history (aligns `engineering-standards.md` §13; not repeated in
depth here).

**Rules.**
- Trunk-based: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>` off `main`; short-lived.
- **Never commit or push unless explicitly asked** (repo policy); never push to `main`; branch
  protection requires green CI + review (2 reviewers for Tier-0/security/evidence/crypto).
- Conventional Commits; imperative subject; body says *why*; footer references the ADR/task;
  AI-assisted commits carry the required co-author trailer.
- Squash-merge to keep `main` linear; never merge red CI; never `--no-verify` or bypass signing
  unless explicitly authorized.

**Workflow.** `git switch -c feat/<slug>` → commit in logical steps → `git push -u origin` (when
asked) → open PR → address review → squash-merge on green.

**Anti-patterns.** Long-lived divergent branches; force-push over an approved review that changes
scope; mixed-purpose commits.

**Common mistakes.** Non-conventional subject; branching from a stale `main`; amending a pushed commit
others based work on.

**Review checklist.**
- [ ] Short-lived, single-purpose branch; Conventional Commits; correct reviewer count.
- [ ] Green CI before merge; linear history preserved.

---

## 5. Working with ADRs

**Purpose.** Keep architecturally significant decisions governed and traceable (governance §2). The
architecture is frozen (v1.0) — ADR work is now the exception, not the routine.

**Rules.**
- The 14 approved ADRs are **immutable inputs**. Implement to them; do not reinterpret or "improve"
  them in code.
- A new ADR is written **only** when an *implementation blocker* is discovered — a case where the
  frozen docs cannot be satisfied as written. It is raised, not decided unilaterally.
- MUST NOT introduce a new datastore, module boundary, external dependency, protocol, or pattern
  without an ADR (governance §"Conventions"). If you feel the urge, stop and raise it.
- Code implementing an ADR references it (module README / docstring); CI's ADR-compliance intent
  (governance §11) means an undocumented decision in code is a defect.

**Workflow (blocker path).**
1. Stop implementation at the blocker.
2. Write a short problem statement: what the frozen doc says, why it can't be met, options.
3. Draft an ADR using the governance §2 template (Status `Proposed`, Criticality Tier, Quality-Gate
   checklist, threat model if Tier-0/1, alternatives, migration/rollback).
4. Route to ARB (+ the relevant board); do not build past the blocker until it is `Accepted`.
5. On acceptance, implement; on rejection, conform to the existing design.

**Examples.** "The frozen `api-design.md` envelope has no field for X that this endpoint requires" →
raise an ADR proposing the envelope change; do not add the field to code first.

**Anti-patterns.** Silent design change in code; "temporary" deviation from an ADR; adding a
dependency and documenting it "later."

**Common mistakes.** Treating a preference as a blocker; implementing past a blocker "to keep moving";
editing an approved ADR in place instead of superseding it.

**Review checklist.**
- [ ] No new datastore/boundary/dependency/pattern without an Accepted ADR.
- [ ] Code references the ADR it implements; no silent design change.

---

## 6. Working with architecture boundaries

**Purpose.** Keep bounded contexts genuinely bounded so extraction stays a folder move (INV-5).

**Rules.**
- Respect the import DAG (`entrypoints` → `modules.*` → `platform` → `shared`); `platform` never
  imports a module. Enforced by `make lint-imports` (`import-linter` contracts in `pyproject.toml`).
- Cross-module **reads** go through the target module's `public.py`; cross-module **state changes**
  are event-mediated (outbox → dispatcher → handler). No synchronous cross-module writes.
- Each module owns one Postgres schema; never query another module's tables; **no cross-schema FK**.

**Workflow.**
1. Before adding an import across a boundary, ask: is this a read (→ `public.py`) or a state change
   (→ event)?
2. Add the dependency the sanctioned way.
3. `make lint-imports` to prove the DAG still holds; add/extend an architecture test if a new boundary
   rule is exercised.

**Examples.**
```python
from sentinelai.modules.ingestion.public import evidence_exists   # GOOD: read via public API
# vs. publishing an event to cause a change in another module (never call it synchronously to mutate)
```

**Anti-patterns.** `from ...modules.other.repository import X`; a "quick" cross-module write;
`platform` importing a domain type.

**Common mistakes.** A relative import that sneaks a boundary crossing past review; reading another
module's tables directly; treating `investigation`'s cross-domain read exception as a general license.

**Review checklist.**
- [ ] `make lint-imports` green; only sanctioned cross-boundary access.
- [ ] Reads via `public.py`; state changes via events; no cross-schema FK.

---

## 7. Working with modules

**Purpose.** A consistent shape so any engineer can navigate any module (guide Part 1; standards §1).

**Rules.**
- A module owns: `router.py` (parse + delegate only), `service.py` (business orchestration),
  `repository.py` (persist only), `models.py`, `schemas.py`, `events.py` (publish/consume wiring),
  `jobs.py`, `uow.py` (concrete UoW subclass with repositories + `OutboxWriter`), `public.py`,
  `migrations/`.
- Business logic lives in the service/aggregate, never in `router.py` or `repository.py`.
- The module's concrete `UnitOfWork` subclass is what binds its repositories and outbox into one
  transaction (guide Part 3; ADR-0005).
- New cross-module surface is added to `public.py` (reads) or the event catalog (facts) — never by
  widening internals.

**Workflow.** Locate the module under `src/sentinelai/modules/<name>/`; change the right layer; keep
the router thin; wire events in `events.py`; register consumers via `register_consumers(dispatcher)`
(called by the composition root).

**Examples.** Adding a read for another module → add a typed function to `public.py` returning a DTO,
not the ORM row.

**Anti-patterns.** Logic in the router; a repository that enforces a business rule; exposing
`models.py` through `public.py`.

**Common mistakes.** Forgetting to register a new consumer; a module UoW that doesn't attach its
outbox; returning an ORM entity from `public.py`.

**Review checklist.**
- [ ] Correct layer holds the change; router thin; repository persist-only.
- [ ] Cross-module surface via `public.py`/events; consumers registered.

---

## 8. Adding a new use case

**Purpose.** Add business behavior the DDD/transaction way (standards §4, §6; ADR-0005/0011).

**Rules.**
- The invariant lives in the **aggregate**; the **application service** orchestrates (load → call
  aggregate method → publish via outbox → return DTO); the **entrypoint** owns the transaction.
- A fact worth announcing is published to the outbox **in the same transaction** as the write.
- Never invent a table/field/event/endpoint not documented in the frozen data/API/event docs (guide
  "Mandatory Implementation Rules" #1).

**Workflow.**
1. Confirm the behavior in `prd.md` (what) and the relevant module design.
2. Write the failing unit test for the aggregate invariant.
3. Add/extend the aggregate method to enforce the rule.
4. Add the application-service method (orchestration only).
5. Add the outbox publish if a fact results; add the event to the catalog (event-driven §25).
6. Wire the entrypoint (endpoint §9 or job) to open/commit the UoW.
7. `make check`; update docs.

**Examples.**
```python
# service orchestrates; aggregate enforces; entrypoint commits
async def close_case(self, case_id: UUID) -> Case:
    case = await self._cases.get(case_id)      # load
    case.close()                               # aggregate enforces the rule + raises domain event
    await self._cases.add(case)                # persist
    await self._outbox.publish(event_type="case.status_changed", ...)  # same transaction
    return case
```

**Anti-patterns.** Rule in the service; `commit()` in the service; publishing outside the transaction;
inventing a field to make it work.

**Common mistakes.** Missing the redelivery-safe handler on the consuming side; forgetting the catalog
entry; returning the aggregate/ORM instead of a DTO.

**Review checklist.**
- [ ] Invariant in the aggregate; service orchestrates; entrypoint commits once.
- [ ] Outbox publish in-transaction; event in the catalog; DTO returned.

---

## 9. Adding a new API endpoint

**Purpose.** Extend the REST surface within the frozen `api-design.md` contract.

**Rules.**
- Endpoint MUST already be documented (or be documented in `api-design.md` in the same change — guide
  #1). Resource-oriented URL under `/api/v1`; correct status code; standard envelope; Pydantic
  request + `...Response` (never an ORM model).
- Router parses + delegates to one service method; UoW opened/committed via a `Depends`; no business
  logic, no `commit` in the router.
- Retryable mutations honor `Idempotency-Key` (ADR-0012); authz declared (`require_role` /
  `require_case_access`); review-status mutations are non-optimistic (PRD FR-7.3).

**Workflow.**
1. Confirm/author the endpoint spec in `api-design.md`.
2. Add request/response schemas in the module's `schemas.py`.
3. Add the route in `router.py`, delegating to the service; declare authz + `response_model`.
4. Add integration test (happy + failure + authz + idempotency where applicable).
5. Verify OpenAPI reflects it (`/openapi.json`); `make check`.

**Examples.**
```python
@router.post("/api/v1/cases", status_code=201, response_model=CaseResponse)
async def open_case(body: OpenCaseRequest, uow: CaseUoW = Depends(get_case_uow),
                    _=Depends(require_role("investigator"))) -> CaseResponse:
    async with uow:
        case = await CaseService(uow.cases, uow.outbox).open_case(body.title)
        await uow.commit()
    return CaseResponse.from_domain(case)
```

**Anti-patterns.** `200` with an error body; returning an ORM row; business logic in the route;
unversioned path; missing authz.

**Common mistakes.** Forgetting idempotency on a retryable POST; leaking a DB error; not updating
`api-design.md`; unbounded list endpoint.

**Review checklist.**
- [ ] Documented in `api-design.md`; `/api/v1`; correct status + envelope; DTO response.
- [ ] Thin router; UoW at boundary; authz declared; idempotency where needed.

---

## 10. Adding a new event

**Purpose.** Extend the event backbone without breaking consumers (event-driven §; ADR-0006/0007).

**Rules.**
- Name `<module>.<past-tense-fact>`; add it to the event catalog (§25) in the same change.
- Publish via `OutboxWriter.publish` in the business transaction; consumers claim the inbox first and
  are idempotent; the envelope is signed on write and verified before dispatch (ADR-0007).
- Payload evolution is additive/back-compatible within a major (`event_version` semver); never remove
  or retype a field — add a new event or a new major instead. Never rename a published `event_type`.

**Workflow.**
1. Add the event to the catalog (publisher, consumers, meaning, version).
2. Define the `...Payload` DTO in the producing module.
3. Publish it in the producing service (in-transaction).
4. Add each consumer handler (inbox claim → side effect → mark processed) and register it.
5. Test: producer writes outbox in-transaction; consumer is redelivery-safe; retry→dead-letter holds.

**Examples.**
```python
await self._outbox.publish(event_type="evidence.linked_to_case", aggregate_type="case",
                           aggregate_id=case.id, payload=payload, correlation_id=corr,
                           actor_type="user")
```

**Anti-patterns.** Present-tense/unprefixed name; publishing outside the transaction; removing a
payload field; non-idempotent handler; renaming an event.

**Common mistakes.** Missing catalog entry; side effect before the inbox claim; assuming global
ordering across aggregates.

**Review checklist.**
- [ ] Name past-tense + in catalog; publish in-transaction; signed envelope.
- [ ] Handler idempotent (inbox-first); payload change back-compatible; retry/dead-letter intact.

---

## 11. Database change workflow

**Purpose.** Evolve schema-per-module safely (database-design §; ADR-0004/0005).

**Rules.**
- Change goes in the module's own schema; **no cross-schema FK**; inter-schema refs are UUID columns
  validated in the app layer.
- Evidentiary tables are INSERT/SELECT-only (ADR-0004) — no `UPDATE`/`DELETE`; supersession is
  append-only.
- Models set `__table_args__={"schema": "<module>"}`; constraint/index names come from
  `db/base.py:NAMING_CONVENTION`. Index every filtered column and the outbox drain path; justify each
  index.
- Every schema change ships with a migration (§12) and its tested `downgrade()`.

**Workflow.**
1. Change the ORM model in `models.py` (or add the table).
2. Generate/author the migration in the module's `migrations/` (§12).
3. `make migrate` on the dev DB; verify shape.
4. Add/extend repository methods (persist-only); tests.
5. `make check` (architecture test asserts no cross-schema FK / no raw SQL).

**Anti-patterns.** Cross-schema FK "for convenience"; `UPDATE` on an evidentiary table; an index with
no rationale; querying another module's table.

**Common mistakes.** Forgetting the schema on a new model; missing index on a hot filter; a migration
without a real downgrade.

**Review checklist.**
- [ ] Change in the owning schema; no cross-schema FK; evidentiary tables append-only.
- [ ] Migration present with tested `downgrade()`; indexes justified.

---

## 12. Migration workflow

**Purpose.** Linear, reversible, DAG-ordered migrations (guide; deployment rule 5).

**Rules.**
- Per-module migration directories (see `alembic.ini` `script_location` per module); applied in
  module-DAG order (`platform` → ingestion → domain modules → investigation → notification) via
  `scripts/migrate.sh` / `make migrate`.
- Every migration has a real, tested `downgrade()` (never `pass`). Naming `YYYYMMDDNNNN_<slug>.py`.
- The generic per-schema `outbox_events`/`inbox_events` tables are created by hand-written migrations
  (not autogenerate). In production, migrations run as ArgoCD PreSync hooks — **never** `alembic`
  by hand against a real environment.

**Workflow.**
```bash
# author the revision in the module's migrations/versions/, upgrade + downgrade both implemented
make migrate                     # applies all modules in DAG order on the dev DB
alembic ... downgrade -1         # (per-module) verify the downgrade works
make test                        # migration round-trip test: upgrade head -> downgrade base
```

**Examples.** A new column: `op.add_column("case_management", ...)` in `upgrade`; the exact inverse in
`downgrade` — verified locally before PR.

**Anti-patterns.** `downgrade(): pass`; autogenerated outbox/inbox tables; applying out of DAG order;
manual `alembic upgrade` against staging/prod.

**Common mistakes.** Downgrade that doesn't fully invert; a migration that assumes another module's
schema exists out of order; forgetting the round-trip test.

**Review checklist.**
- [ ] Real, tested `downgrade()`; correct module dir; DAG order preserved.
- [ ] `upgrade head` → `downgrade base` round-trip green; no manual prod apply.

---

## 13. Writing tests

**Purpose.** Prove invariants and contracts, not just lines (standards §11; governance §4).

**Rules.**
- Layers: unit (fakes) · integration (Testcontainers) · contract (same suite vs every adapter) ·
  architecture (`import-linter` + boundary/SQL/secret asserts) · performance (Tier-0/1 benchmarks).
- Coverage floors: `platform`/crypto/evidentiary/auth (Tier-0) **≥ 90%** + 100% of security/
  evidentiary invariants tested; Tier-1 ≥ 80%; Tier-2 ≥ 60%.
- Deterministic: no internet, no wall-clock assertions, no sleeps-as-sync; async via `pytest-asyncio`
  (`asyncio_mode=auto`). Test data is never real PII.
- Every Tier-0 change tests: happy path, each failure/permission path, and each invariant
  (append-only, idempotency/redelivery, crypto tamper/downgrade, legal-hold refusal).

**Workflow.** Write the failing test first → implement → add integration/contract where a real
dependency or a new adapter is involved → `make test` → confirm coverage floor.

**Examples.**
```python
async def test_handler_is_redelivery_safe() -> None:
    await handle(event, uow); await handle(event, uow)   # second delivery is a no-op (inbox)
    assert side_effect_count() == 1
```

**Anti-patterns.** Mocking the unit under test; asserting on log strings; internet in a test; coverage
padding; real PII fixtures.

**Common mistakes.** Missing the redelivery test; not running the contract suite for a new adapter;
flaky time assertion.

**Review checklist.**
- [ ] Right layers present; coverage floor met; invariants explicitly tested.
- [ ] Deterministic; no PII; missing containers skip (not pass).

---

## 14. Running local quality gates

**Purpose.** Reproduce CI locally so nothing surprising fails in the pipeline.

**Rules.** `make check` MUST pass before push. It runs the same gates CI enforces.

**Workflow.**
```bash
make format        # ruff format + ruff --fix
make lint          # ruff check
make typecheck     # mypy strict
make lint-imports  # import-linter DAG + platform-agnostic contracts
make test          # pytest (+ coverage)
make check         # all of the above in one shot — the local CI mirror
```

**Anti-patterns.** Pushing without `make check`; disabling a check to get green; blanket
`# type: ignore` / `# noqa`.

**Common mistakes.** Formatting locally with a different tool than `ruff format`; skipping
`lint-imports` (boundary regressions hide there); coverage dropping below the floor unnoticed.

**Review checklist.**
- [ ] `make check` green locally; no suppressed check; coverage floor held.

---

## 15. CI failure resolution

**Purpose.** Fix the root cause, never mask it.

**Rules.** A red gate is fixed by fixing the code/test/doc — never by weakening the gate. A genuine
false positive needs a labeled, time-boxed, board-approved exception in the risk register (governance
§11), not a silent disable.

**Workflow (by gate).**
- **ruff/format** → `make format`, re-lint. **mypy** → fix the type; justify any unavoidable ignore
  with a code + reason. **import-linter** → you crossed a boundary — route via `public.py`/event, not
  a suppression. **pytest/coverage** → fix the failing behavior or add the missing invariant test;
  raise coverage by testing, not by excluding files. **security/secret scan** → remove the secret,
  rotate if it ever touched a remote, use Vault+ESO. **SBOM/dep scan** → replace/upgrade the offending
  dep (ADR-gated if it's a new one). **container/cosign** → fix the base image / signing config.
  **migration round-trip** → implement the missing/incorrect `downgrade()`.

**Anti-patterns.** `# noqa`/`type: ignore`/`--no-verify` to get green; excluding a file from coverage;
committing a secret then "removing" it in a later commit (it's in history — rotate).

**Common mistakes.** Treating an import-linter failure as a lint nit; padding coverage; not rotating a
leaked secret.

**Review checklist.**
- [ ] Root cause fixed, gate not weakened; any exception is recorded and approved.
- [ ] Leaked secret rotated; boundary fix routed properly.

---

## 16. Debugging standards

**Purpose.** Diagnose with the platform's own observability, safely.

**Rules.**
- Debug via **structured logs + correlation/trace ids + metrics + `/readyz`** — not `print`, not a
  debugger left in code. Never log a secret/PII to chase a bug.
- Reproduce locally against the dev stack or a Testcontainers integration test before changing code.
- Read the correlation id from the response header (`X-Correlation-Id`) and grep logs by it.
- A dependency issue shows in `/readyz` (which check failed) and USE metrics — start there.

**Workflow.** Reproduce → capture the correlation id → trace it across `http` and `worker` logs →
form a hypothesis → write a failing test that reproduces it → fix → confirm the test passes and
`make check` is green.

**Examples.**
```bash
curl -i localhost:8000/api/v1/... | grep -i x-correlation-id   # then filter logs by that id
```

**Anti-patterns.** `print`-debugging; logging a token to "see it"; fixing without a reproducing test;
attaching a debugger in committed code.

**Common mistakes.** Ignoring the worker process when the bug is in event handling; assuming exactly-
once and missing a redelivery bug; not checking `/readyz` first.

**Review checklist.**
- [ ] Reproduced by a test; no secret/PII logged; fix has a regression test.

---

## 17. Performance validation workflow

**Purpose.** Keep NFR budgets green by extension, not rewrite (system-design §10; standards §15).

**Rules.**
- Async end-to-end; no N+1 (eager-load with `selectinload`); stream large objects; bounded pages;
  one pool per process (HPA bounds respect Postgres `max_connections`).
- Tier-0/1 changes carry a benchmark regression test; latency/throughput budgets are asserted, and
  RED/USE metrics expose regressions.

**Workflow.** Identify the hot path → add/adjust the benchmark → measure against baseline → fix
N+1/blocking/buffering → confirm no regression → keep the metric names consistent (standards §7).

**Examples.**
```python
stmt = select(Case).options(selectinload(Case.findings))   # avoids N+1 on a hot read
```

**Anti-patterns.** Blocking I/O on the async path; a query in a loop; buffering a large blob; a new
pool per request; unbounded list.

**Common mistakes.** Lazy relationship access; missing the benchmark; oversized HPA bounds exhausting
the DB pool.

**Review checklist.**
- [ ] No N+1/blocking/buffering; pooling correct; Tier-0/1 benchmark present and green.

---

## 18. Security review workflow

**Purpose.** Every security-relevant change is reviewed against the authoritative security model
(`security-architecture.md`; governance §5, §12).

**Rules.**
- A change touching authn/authz, crypto, secrets, PII, evidence integrity, or upload/storage triggers
  the Security Review Board path and needs a threat model (STRIDE + abuse cases) for Tier-0/1.
- Enforce: no secret in logs/exceptions/responses; crypto only via `platform.crypto`; parameterized
  queries; authz declared/enforced (RBAC/ABAC); PII masked; deletion/purge checks legal hold;
  evidentiary tables append-only. Never weaken/bypass an authz check without an explicit, labeled,
  reviewed exception.
- Two reviewers for any Tier-0/security/evidence/crypto change.

**Workflow.** Classify the change's tier → write/attach the threat model → self-check against
standards §9 → request SRB (+ DFRB if evidentiary, AIRB if AI) → resolve findings → merge only on
sign-off.

**Anti-patterns.** "Temporary" authz bypass; a home-rolled crypto helper; a purge path that skips
legal hold; a secret in a log to debug.

**Common mistakes.** Missing the threat model on a Tier-0 change; PII leaking into a metric label or
log; naming an algorithm at a call site instead of using the policy engine.

**Review checklist.**
- [ ] Threat model for Tier-0/1; secrets/crypto/authz/PII/legal-hold rules met; 2 reviewers; board
      sign-off where required.

---

## 19. Documentation update workflow

**Purpose.** Docs and code never drift (governance §"Working style").

**Rules.**
- Any change to a documented behavior updates the authoritative doc **in the same PR**: endpoint →
  `api-design.md`; event → the catalog (event-driven §25); schema → `database-design.md`; evidence
  shape/custody → `canonical-evidence-model.md`; a decision → an ADR; a standard → `engineering-
  standards.md`.
- Docstrings state the contract; comments explain *why*; no `TODO`/stub-as-done.

**Workflow.** Make the code change → update the matching doc/catalog → cross-check the frozen source
still holds → include both in the same PR (a doc-sync review item).

**Anti-patterns.** "Docs to follow in a later PR"; a code change that silently diverges from a frozen
contract; a stub with a docstring claiming completeness.

**Common mistakes.** Adding an event in code but not the catalog; changing an error code without
`api-design.md`; leaving `architecture.md` stale after a decision.

**Review checklist.**
- [ ] Authoritative doc/ADR/catalog updated in the same PR; docstrings state contracts; no TODO.

---

## 20. Release preparation workflow

**Purpose.** Ship a validated, revertible release (governance §12–13; deployment rules).

**Rules.**
- GitOps only (ArgoCD reconciles a merged change) — no manual `kubectl`/`alembic` against a real
  environment. Migrations run as PreSync hooks in module-DAG order. Images are cosign-signed from an
  approved base; an SBOM ships with the build.
- A release carries a **validated rollback exercised in staging** and passes the Production Readiness
  Review before production trust.

**Workflow.** Confirm Module DoD for everything in scope → CI produces signed image + SBOM → deploy to
staging via GitOps → exercise the rollback in staging → PRR sign-off → promote via GitOps → tag
(semver) with notes referencing the shipped ADRs/waves.

**Anti-patterns.** Manual prod change; unsigned image; migrations out of order or by hand; releasing
without a tested rollback.

**Common mistakes.** Skipping the staging rollback drill; missing SBOM; tag notes that don't reference
what shipped.

**Review checklist.**
- [ ] Signed image + SBOM; GitOps deploy; PreSync migrations in order; staged rollback validated; PRR
      passed; semver tag.

---

## 21. AI coding assistant workflow (mandatory)

**Purpose.** AI assistants (Claude Code, Cursor, Codex, ChatGPT, and any future agent) accelerate
work **inside** the frozen architecture — never around it. An AI assistant has the same obligations as
a human contributor plus stricter guardrails because it generates quickly.

**Rules — the AI hard-noes (each is blocking):**
- AI **MUST NOT invent** an API, endpoint, event, table, column, field, or config key. If it isn't in
  `api-design.md` / event catalog / `database-design.md` / `config.py`, stop and flag — do not add it
  to code first (guide "Mandatory Implementation Rules" #1).
- AI **MUST NOT bypass architecture:** no cross-boundary deep import, no cross-module synchronous
  write, no logic in routers/repositories, no `commit` in a service, no cross-schema FK.
- AI **MUST NOT introduce a dependency** (or datastore, protocol, pattern, tool) not already declared
  in `pyproject.toml` / the frozen docs. A needed dependency is an ADR (§5), raised, not added.
- AI **MUST refuse architecture changes.** If a prompt asks to redesign, re-pattern, restructure the
  repo, or "improve" a frozen decision, the assistant refuses and points to the frozen doc/ADR, and —
  if the change is genuinely warranted — asks for an ADR instead of doing it.
- AI **MUST ask for an ADR when required** (new datastore/boundary/dependency/pattern, or a frozen
  contract cannot be met as written). It stops at the blocker and surfaces it; it does not build past
  it.
- AI **MUST NOT silently modify a frozen contract** (API envelope, event name/payload, DB schema
  rules, security invariants). Any such change is proposed explicitly, with the doc/ADR update in the
  same change, and is called out for human review.
- AI **MUST NOT weaken a gate or a security control** to make something pass (no `# noqa`/
  `type: ignore`/`--no-verify`/coverage exclusion/authz bypass to get green).
- AI **MUST NOT fabricate results.** It never claims tests passed it didn't run, never invents
  benchmark numbers, never reports a gate green without running it. Unknowns are stated as unknown.
- AI **MUST keep secrets safe:** never print/commit a secret, never add a real credential, never log
  PII.
- AI **MUST honor human-in-the-loop:** it proposes; a human reviews and merges. It does not push to
  `main`, and it commits/pushes only when explicitly asked (repo policy).

**Workflow (every AI-assisted task).**
1. **Read first:** governance → the relevant ADR(s) → the authoritative doc for the area → standards →
   this workflow. Prior sections tell it *where* to look.
2. **Verify, don't assume:** confirm the endpoint/event/table exists in the frozen docs before
   coding; if a recalled fact names a file/flag, check it still exists.
3. **Detect conflict → STOP:** if the prompt conflicts with a frozen doc (wrong stack, hexagonal
   re-layering, a new pattern, an invented API), the assistant stops, explains the conflict against the
   specific frozen doc, and asks before proceeding — it does not comply silently.
4. **Implement to the standards**, smallest change that satisfies the frozen spec.
5. **Run the real gates** (`make check`) and report actual results; update docs/catalog in the same
   change.
6. **Surface everything ambiguous or blocked** to the human, including any place it had to guess.

**Examples.**
```
Prompt: "Add a `priority` field to the case event payload and rename the event."
AI (correct): "Renaming a published event and changing its payload are frozen-contract changes
(event-driven §, ADR-0007 back-compat rule). I won't do this silently. Options: add a new event, or
an additive optional field with a version bump — both need the catalog updated in the same change.
Which do you want, and should I draft the catalog entry?"
```
```
Prompt: "Just use SQLModel here, it's simpler."
AI (correct): "The stack is fixed by ADR-0002 (SQLAlchemy 2.0 async); SQLModel is not permitted and
isn't in pyproject.toml. Introducing it would need an ADR. I'll implement this with the frozen stack."
```

**Anti-patterns.** An assistant that invents a plausible endpoint; adds a helpful library; refactors
the repo layout unprompted; reports "all tests pass" without running them; complies with a
stack-changing prompt because it was asked confidently.

**Common mistakes.** Guessing a field name instead of checking `database-design.md`; "improving" a
frozen pattern; skipping the doc update; pushing without being asked.

**Review checklist (for the human reviewing AI output).**
- [ ] No invented API/event/table/field/dep; no boundary bypass; no frozen-contract change slipped in.
- [ ] Gates actually run and truthfully reported; docs/catalog updated in the same change.
- [ ] Any ambiguity/assumption surfaced; nothing pushed without authorization.

---

## 22. Mandatory pre-merge checklist

Applied by the author before requesting review and by the reviewer before approving (superset lives
in `engineering-standards.md` §16).

**Correctness & boundaries**
- [ ] Invariants in aggregates; router thin; service orchestrates; repository persist-only.
- [ ] `make lint-imports` green; cross-module reads via `public.py`, state changes via events; no
      cross-schema FK.

**Transactions & events**
- [ ] UoW at the entrypoint; service never commits; outbox publish in-transaction.
- [ ] Handler claims inbox first and is idempotent; event named past-tense + in catalog; payload
      back-compatible; retry→dead-letter intact.

**API & data**
- [ ] Endpoint documented in `api-design.md`; `/api/v1`; correct status + envelope; DTO response;
      idempotency where retryable; authz declared.
- [ ] Migration present with tested `downgrade()`; evidentiary tables append-only; indexes justified.

**Security & evidence**
- [ ] No secret/PII in logs/exceptions/responses; crypto via `platform.crypto`; parameterized queries;
      legal-hold checked on any deletion/purge.

**Quality & docs**
- [ ] `make check` green; coverage floor met; invariants tested; no `TODO`/stub-as-done.
- [ ] Docs/ADR/catalog updated in the same PR; Conventional Commit; correct reviewer count (2 for
      Tier-0/security/evidence/crypto).

---

## 23. Developer Definition of Ready

A work item is **Ready** to start only when:
- [ ] It traces to an FR/NFR/SR in `prd.md` (or one is drafted) with testable acceptance criteria.
- [ ] The frozen design for the area exists and is understood (the relevant ADR/doc identified).
- [ ] It needs **no** new datastore/boundary/dependency/pattern — or the required ADR is already
      `Accepted` (if not, it is not Ready; raise the ADR first).
- [ ] Its criticality tier is known (sets reviewer count, threat-model need, coverage floor).
- [ ] Scope is small and single-purpose (one use case / endpoint / event / migration).

If any box is unchecked, the item is **not** Ready — resolve it before writing code (prevents the
"invent-as-you-go" failure mode).

---

## 24. Developer Definition of Done

Nested, per `engineering-standards.md` §17 — restated operationally.

**Change/PR is Done when:**
- [ ] Implements the frozen spec; no redesign/new-tech/new-ADR (or a blocker was raised as one).
- [ ] `make check` green (ruff + mypy + import-linter + tests); coverage floor met; invariants tested;
      no TODO/stub-as-done.
- [ ] Docs/ADR/catalog updated in the same change; secrets clean; logs structured + PII-safe.
- [ ] Migration (if any) has a tested `downgrade()`; endpoint/event/schema changes reflected in the
      authoritative doc.
- [ ] PR is focused, Conventional-Commit'd, review-approved (2 for Tier-0), CI fully green.

**Module Done** additionally: public interface documented, events in the catalog, boundary enforced by
architecture tests, Tier-appropriate coverage/benchmarks, threat model for Tier-0/1 surfaces.

**Release Done** additionally: SBOM + signed image, PreSync/DAG-ordered migrations, validated staging
rollback, PRR passed. A "Done" claim is a statement of fact — if a step was skipped, say so; it is not
Done.

---

## 25. Repository golden rules

The short list every contributor internalizes. Each maps to a frozen source.

1. **The architecture is frozen.** Build to it; never redesign it in code. New structural decisions
   are ADRs (governance §2).
2. **Never invent an API, event, table, or field.** If a frozen doc doesn't document it, stop and flag
   (guide #1).
3. **Boundaries are law.** `platform` imports no module; cross-module reads via `public.py`, state
   changes via events; no cross-schema FK (`make lint-imports`).
4. **Transaction at the entrypoint; outbox in the same transaction; inbox-first, idempotent handlers**
   (ADR-0005/0006/0007).
5. **Rich aggregates, thin routers, persist-only repositories** (ADR-0011; standards §4).
6. **Fail closed. Never bypass a security or authz control.** Crypto only via `platform.crypto`
   (ADR-0009); evidentiary tables append-only; legal-hold checked before any deletion (ADR-0004,
   security-architecture).
7. **No secret or PII in the repo, logs, exceptions, or responses.** Secrets via Vault+ESO; `.env` is
   dev-only.
8. **No new dependency/tech without an ADR.** The stack is fixed (ADR-0002).
9. **`make check` is the bar.** A change that can't pass every gate is not done; never weaken a gate to
   pass.
10. **Docs and code move together.** Update the authoritative doc/ADR/catalog in the same change.
11. **Tell the truth about status.** Never claim a gate green you didn't run; state unknowns and
    skipped steps plainly.
12. **Humans decide.** AI proposes; a human reviews and merges; commit/push only when asked.

---

*Keep this document synchronized with the frozen corpus. If an implementation blocker forces a design
change, raise an ADR (governance §2) and update the affected frozen doc in the same change — never let
the daily workflow drift from the architecture it serves.*
