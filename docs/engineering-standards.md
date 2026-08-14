# SentinelAI — Engineering Standards Manual

**Status:** Mandatory coding handbook. Every engineer (human or AI) writing code in this repository
follows it. It is **not** architecture and **not** planning — it is *how we write the code the
frozen architecture calls for*.

**Precedence (when documents conflict):** `engineering-governance.md` → approved ADRs →
`architecture.md` → `implementation-wave-1.md`. This manual restates and operationalizes those; it
never overrides them. Nothing here introduces a new technology, a new ADR, or an architectural
change — all of it is derived from the frozen corpus (`backend-implementation-guide.md` is the
*how* authority and is cited throughout as "guide PartN").

**How to read each section:** every topic gives **Purpose · Rules · Examples · Anti-patterns ·
Common mistakes · Review checklist.** Examples are illustrative snippets of *style*, not product
code to copy wholesale. When a rule says MUST it is CI- or review-blocking; SHOULD is a strong
default that needs a justified exception.

---

## 1. Repository standards

**Purpose.** Keep bounded contexts genuinely bounded so module extraction stays a folder move, and
so `platform` never learns about a domain. Boundaries are enforced by tooling (`import-linter`,
`ruff` TID), not goodwill.

**Rules.**
- MUST place code in the correct layer. Import DAG, highest→lowest: `entrypoints` → `modules.*`
  (with `investigation` above the domain modules) → `platform` → `shared`. A layer imports only
  **lower** layers.
- `platform` MUST NOT import `modules` (the `platform is domain-agnostic` forbidden contract).
- `shared` MUST NOT import `platform` or `modules` — it is pure types/utilities with no I/O.
- Cross-module access MUST go through the target module's `public.py` **or** the event bus. Never
  import another module's `models.py`, `repository.py`, `service.py`, or `uow.py`.
- Cross-module **public-interface calls are read-only** (INV-5, validation report). Any cross-module
  **state change** is event-mediated (outbox → dispatcher → the other module's handler). A
  synchronous cross-module write is forbidden — it reintroduces a distributed transaction on
  extraction.
- `public.py` exposes only DTOs (Pydantic/dataclasses) and narrow function/Protocol signatures —
  never an ORM model, a `Session`, or a repository.
- Absolute imports only (`from sentinelai.platform.db.session import get_session`). No relative
  cross-module imports (`ruff` TID251/TID252).
- Each module owns exactly one Postgres schema; no module queries another's tables.

**shared vs platform.** `shared` = framework-free primitives usable by anyone (value types, pure
helpers, constants). `platform` = cross-cutting *infrastructure* with I/O and framework glue (db,
events, crypto, cache, storage, security, observability). If it touches a connection, a request, or
a secret, it is `platform`, not `shared`.

**Examples.**
```python
# GOOD — case_management asks ingestion a question through its public API (read-only)
from sentinelai.modules.ingestion.public import evidence_exists  # returns a bool DTO answer

# GOOD — announcing a fact across modules: publish, don't call
await self.outbox.publish(event_type="case.status_changed", ...)  # dispatcher delivers it
```
```python
# BAD — deep import of another module's internals
from sentinelai.modules.ingestion.repository import EvidenceRepository   # boundary violation
# BAD — platform importing a domain module
from sentinelai.modules.forensics.models import Artifact                 # forbidden contract
```

**Anti-patterns.** Shared "utils" grab-bag that quietly imports `platform`; a module reaching into
another's repository "just to read"; `public.py` returning an ORM entity.

**Common mistakes.** Putting a DB helper in `shared`; a relative import that slips a cross-module
dependency past review; adding a synchronous write across modules because "it's one process anyway."

**Review checklist.**
- [ ] Every new import respects the DAG; `import-linter` passes.
- [ ] No cross-module deep import; cross-module reads go through `public.py`.
- [ ] No cross-module synchronous write (state change is event-mediated).
- [ ] New shared code is I/O-free and framework-free.
- [ ] New module code sets its own schema; no cross-schema FK.

---

## 2. Python standards

**Purpose.** Uniform, strictly-typed, async-correct Python that mypy `strict` can fully verify.

**Rules.**
- Target **Python 3.12+**. `from __future__ import annotations` at the top of every module.
- **mypy `strict`** MUST pass with no new `type: ignore`. An unavoidable ignore carries a specific
  code and a one-line reason (`# type: ignore[arg-type]  # upstream stub gap`).
- Every function/method has typed parameters and a return type (`disallow_untyped_defs`).
- **Ports are `typing.Protocol`** (structural), defined in `platform`/module interface files;
  adapters implement them by shape. Business/plumbing code depends on the Protocol, never the
  concrete class.
- **Value objects / events / DTOs are immutable:** `@dataclass(frozen=True, slots=True)` (or a
  Pydantic model with `frozen=True`). `slots=True` on hot value objects.
- **Pydantic v2** for all boundary data (request/response schemas, settings, event payload
  validation). ORM models are SQLAlchemy, not Pydantic — never conflate the two.
- **Enums** (`enum.Enum`/`StrEnum`) for every closed set (status, category, actor type). Never bare
  strings or magic numbers for a state.
- **UUIDs**: primary keys and all cross-schema references are `uuid` (Python `UUID`, PG `UUID`).
  Generate with `uuid4()`. Cross-schema refs are plain UUID columns — no FK (database-design §5).
- **datetime**: always timezone-aware UTC — `datetime.now(UTC)`. Never naive datetimes; never local
  time. Store `TIMESTAMP(timezone=True)`.
- **`ContextVar`** for request-scoped ambient state (correlation id via structlog contextvars; the
  reserved `tenant_id` ContextVar). Never a module-global mutable for per-request state.
- **Async rules:** everything on the request/job path is `async`. MUST NOT call blocking I/O
  (sync DB drivers, `requests`, `time.sleep`, blocking file reads) inside async code — use `asyncpg`,
  `httpx.AsyncClient`, `asyncio.sleep`, and stream large I/O. CPU-bound work goes to the worker, not
  an inline block. Never create a second DB connection inside a request that already has a session.

**Examples.**
```python
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

class CaseStatus(StrEnum):
    OPEN = "open"; CLOSED = "closed"; ARCHIVED = "archived"

@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    value: float
    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError("confidence must be within [0,1]")
```

**Anti-patterns.** `Any` as a type-checker escape hatch; mutable default args; passing dicts where a
typed DTO belongs; `datetime.utcnow()` (naive); string status compared with `==` on literals.

**Common mistakes.** Forgetting `tzinfo`; a Pydantic model used as an ORM row; a Protocol
implemented by inheritance instead of by shape; blocking call sneaking into an async handler.

**Review checklist.**
- [ ] mypy strict clean; no unjustified `type: ignore`.
- [ ] Closed sets are Enums; ids are UUID; datetimes are tz-aware UTC.
- [ ] Value objects/events are frozen (+ slots where hot).
- [ ] No blocking I/O on an async path; no second connection per request.

---

## 3. Naming conventions

**Purpose.** Names encode role and layer, so a reader knows what a thing *is* from its name.

**Rules & table.**
| Kind | Convention | Example |
|---|---|---|
| Files/modules | `snake_case` | `case_service.py` |
| Classes | `PascalCase` | `EvidenceRepository` |
| Functions/methods/vars | `snake_case` | `record_custody` |
| Constants | `UPPER_SNAKE` | `MAX_UPLOAD_BYTES` |
| Events | `<module>.<past-tense-fact>` | `evidence.ingested`, `case.status_changed` |
| Commands (intent) | imperative verb phrase | `record_evidence`, `open_case` |
| Queries (read) | `get_*` / `list_*` / `find_*` | `get_case`, `list_findings` |
| DTOs (API) | `...Request` / `...Response` | `OpenCaseRequest`, `CaseResponse` |
| Event payload DTO | `...Payload` | `EvidenceIngestedPayload` |
| Repositories | `<Aggregate>Repository` | `CaseRepository` |
| Services (app layer) | `<Aggregate>Service` | `CaseService` |
| Ports (interfaces) | capability noun | `ObjectStorage`, `PasswordHasher`, `CryptoProvider` |
| Adapters (impl) | `<Tech><Port>` | `MinioObjectStorage`, `Argon2PasswordHasher` |
| Exceptions | `...Error` | `ConflictError`, `KmsUnavailable` |
| DB constraints/indexes | `db/base.py` `NAMING_CONVENTION` | `ix_…`, `uq_…`, `pk_…` |

- Event **names are the contract** — past tense, module-prefixed, never renamed once published (add
  a new event instead). Register every event in the event catalog (event-driven §25) in the same
  change that introduces it.
- Repository methods name the persistence action (`add`, `get`, `list`), never the business rule.
- Service methods name the use case (`open_case`), never CRUD (`update_case_row`).

**Examples.**
```python
class CaseResponse(BaseModel): ...              # API DTO, never an ORM model
async def list_findings(...) -> list[FindingResponse]: ...   # query
async def confirm_finding(...) -> None: ...     # command / use case
class MinioObjectStorage: ...                   # adapter implements ObjectStorage port
```

**Anti-patterns.** `data`, `info`, `manager`, `helper`, `utils2`; a service method called `process`;
an event named in present tense (`case.status_change`) or without a module prefix.

**Common mistakes.** Naming a port after the tech (`VaultClient` instead of `CryptoProvider`);
suffixing a request DTO `...Model`; renaming an event's `event_type` string.

**Review checklist.**
- [ ] Ports named for capability; adapters named for tech.
- [ ] DTOs suffixed correctly and never expose ORM types.
- [ ] Event name is `<module>.<past-tense>` and is in the catalog.
- [ ] Service = use case names; repository = persistence names.

---

## 4. Domain-Driven Design rules

**Purpose.** Rich aggregates that own their invariants (ADR-0011), a clean hexagonal split, and no
logic leaking into routers or repositories.

**Rules.**
- **Entities** have identity (UUID) and a lifecycle; equality is by id.
- **Value objects** are immutable, validated at construction, equality by value (`IntegrityHash`,
  `ConfidenceScore`, `EvidenceCategory`, `LegalAuthorityRef`). No identity, no setters.
- **Aggregates** are the consistency boundary and the *only* way to mutate their internals. The
  aggregate exposes intent methods (`Evidence.record_custody(...)`, `Case.close()`,
  `Finding.confirm()`) and **refuses illegal transitions**; callers cannot reach inner state
  (ADR-0011). One transaction mutates one aggregate; cross-aggregate consistency is eventual (events).
- **Repositories** persist and reload aggregates — **persistence only**, no business logic, no
  commit. One repository per aggregate root.
- **Factories** build valid aggregates/value objects; invalid input raises at construction — an
  invalid object never exists.
- **Domain services** hold domain logic that doesn't belong to a single aggregate (pure, no I/O).
- **Application services** orchestrate a use case: load via repository, call aggregate methods,
  publish via outbox, and return a DTO. They contain no domain rules and no SQL.
- **Ports & adapters (hexagonal):** the application/domain depends on ports (Protocols); adapters
  (SQLAlchemy repo, MinIO storage, Vault crypto) live at the edge and are injected by the composition
  root.
- **No anemic models:** a class that is only getters/setters with logic living in the service is a
  defect — move the invariant into the aggregate.
- **No fat controllers:** routers parse input, call one application-service method, map the result to
  a response DTO. No business logic, no repository calls, no `commit` in a router.

**Examples.**
```python
# GOOD — aggregate owns the rule; illegal transition is impossible
class Case:
    def close(self) -> None:
        if self._status is not CaseStatus.OPEN:
            raise ConflictError("only an open case can be closed")
        self._status = CaseStatus.CLOSED
        self._raise(CaseStatusChanged(self.id, CaseStatus.CLOSED))
```
```python
# BAD — anemic model + fat controller (logic in the router)
@router.post("/cases/{id}/close")
async def close_case(id: UUID, session=Depends(get_session)):
    case = await session.get(CaseRow, id)
    if case.status == "open":           # rule leaked into the endpoint
        case.status = "closed"
    await session.commit()              # router commits — forbidden (ADR-0005)
```

**Anti-patterns.** Setters on aggregates; services reaching into aggregate internals; repositories
that validate business rules; routers that orchestrate multiple writes.

**Common mistakes.** Returning the aggregate/ORM from the route; putting the ≥1-supporting-evidence
rule for `Finding` in the service instead of the aggregate; a "God service."

**Review checklist.**
- [ ] Invariants live in the aggregate; illegal states are unrepresentable.
- [ ] Repository has no business logic and does not commit.
- [ ] Application service orchestrates only; router is thin; response is a DTO.
- [ ] Value objects validate at construction.

---

## 5. Dependency Injection rules

**Purpose.** Inject interfaces at the edge; never fetch dependencies from a global.

**Rules.**
- **Constructor injection** for services/adapters: dependencies are explicit parameters, typed as
  ports. A class never constructs its own infrastructure.
- **FastAPI `Depends`** is the request-scoped DI mechanism (session, UoW, current principal). The
  **composition root** (`entrypoints/http/main.py:create_app` + lifespan, `entrypoints/worker`
  `on_startup`) is the only place allowed to import concrete adapters and modules.
- **No service locator:** code MUST NOT call a global registry to *fetch* a dependency. `app.state`
  and the worker `ctx` hold process-lifetime singletons created at startup; they are wired via
  `Depends`, not read ad hoc from deep in a service.
- **Lifetimes:** *singleton* (process) — engine, session factory, KMS, object-storage client, Redis
  pool, dispatcher, arq pool (built in lifespan, disposed in `finally`). *scoped* (request) —
  `AsyncSession`/UoW. *transient* — DTOs/value objects. Never share an `AsyncSession` across
  requests or store one on a singleton.
- **Factories** are pure functions of `Settings` (`create_kms`, `build_object_storage`,
  `build_password_hasher`), so tests build isolated instances and override ports with fakes via
  `app.dependency_overrides`.

**Examples.**
```python
# GOOD — constructor injection of a port
class CaseService:
    def __init__(self, cases: CaseRepository, outbox: OutboxWriter) -> None:
        self._cases = cases; self._outbox = outbox
```
```python
# BAD — service locator: reaching for a global singleton
kms = get_global_kms()            # hidden dependency, untestable, forbidden
```

**Anti-patterns.** Importing `settings` deep inside a service to build a client; a singleton holding
a request session; constructing an adapter inside domain code.

**Common mistakes.** Forgetting to dispose a singleton in the lifespan `finally`; overriding a
dependency in tests but leaving the real one wired; a `Depends` that creates a second session.

**Review checklist.**
- [ ] Dependencies are constructor/`Depends`-injected ports, not fetched globals.
- [ ] Only the composition root imports concrete adapters/modules.
- [ ] Correct lifetime; no session on a singleton; singletons disposed on shutdown.

---

## 6. SQLAlchemy standards

**Purpose.** Async, schema-owned, transaction-correct persistence (guide Parts 2–5, ADR-0004/0005).

**Rules.**
- SQLAlchemy 2.0 **async** with `asyncpg`. Never a sync driver, never `session.execute` of a
  string-built SQL — **parameterized queries only** (security §27).
- **Repositories persist only** — typed CRUD on their aggregate; no business rules; no `commit`.
- **Unit of Work at the entrypoint (ADR-0005):** the HTTP dependency / worker job wrapper opens the
  UoW, the service mutates through it, the entrypoint commits once on success and rolls back on any
  exception. **Services never `commit()`/`rollback()`.** Composed service calls share the one
  ambient transaction.
- **Outbox in the same transaction:** `OutboxWriter.publish` writes on the module's session inside
  the business transaction — never a second connection, never after `commit()` (event-driven §16).
- **Lazy loading is off** (`expire_on_commit=False`, no implicit lazy). Load relationships
  explicitly with `selectinload`/`joinedload`; a relationship access that would trigger lazy I/O is
  a bug.
- **Schema ownership:** every model sets `__table_args__ = ({"schema": "<module>"},)`. **No
  cross-schema `ForeignKey`** — inter-schema references are plain UUID columns validated in the app
  layer (database-design §5).
- **Migrations (Alembic):** one linear history; deterministic names via `NAMING_CONVENTION`; each
  migration has a real, tested `downgrade()` (never `pass`); the generic per-schema
  `outbox_events`/`inbox_events` tables are created by hand-written migrations, not autogenerate.
  Applied in module-DAG order as ArgoCD PreSync hooks — never by hand against a real environment.
- **Indexes:** index every FK-like UUID reference actually queried, every status column filtered on,
  and the outbox `(dispatch_status, occurred_at)` drain path. Justify each index in the migration.
- **Evidentiary tables** are INSERT/SELECT-only at the DB-role level (ADR-0004); code MUST NOT
  `UPDATE`/`DELETE` them — supersession is append-only.

**Examples.**
```python
# GOOD — entrypoint owns the transaction; service just mutates
async def endpoint(uow: CaseUoW = Depends(get_case_uow)):
    async with uow:
        await CaseService(uow.cases, uow.outbox).open_case(...)
        await uow.commit()               # the ONLY commit, at the boundary
```
```python
# BAD — N+1 via lazy access, and a service that commits
for case in await repo.list_open():
    print(case.findings)                 # lazy load per row → N+1
await session.commit()                   # in a service — forbidden
```

**Anti-patterns.** String-interpolated SQL; a cross-schema FK "for convenience"; autogenerated
migration with a `pass` downgrade; returning ORM rows from a service.

**Common mistakes.** Accessing a relationship not eager-loaded; committing inside a service; the
outbox write on a different session than the business write.

**Review checklist.**
- [ ] Parameterized queries only; no string SQL.
- [ ] Transaction opened/committed at the entrypoint; service never commits.
- [ ] Outbox write shares the business transaction.
- [ ] Relationships eager-loaded; no lazy access on the path.
- [ ] Schema set; no cross-schema FK; migration has a tested downgrade.

---

## 7. Event-Driven standards

**Purpose.** At-least-once, idempotent, ordered, versioned events whose transport can swap to
Redpanda without touching modules (event-driven §, ADR-0006/0007).

**Rules.**
- **Publishing:** a fact-announcing write publishes via `OutboxWriter.publish` **in the same
  transaction** as the business write. Never publish from a router; never publish after commit;
  never call another module directly to "notify" it.
- **Consuming:** every handler performs the **Inbox claim first** (`InboxGuard.try_claim(event_id,
  handler_name)`) before any side effect; a `False` claim (redelivery) skips silently. Assume
  at-least-once **always** — handlers are idempotent even though the Phase-1 in-process bus could be
  exactly-once.
- **Retry & dead-letter:** the dispatcher retries a failed row with backoff until `max_attempts`,
  then marks it `dead_letter` (logged + `outbox_dead_letter_total` metric). A poisoned event is
  **never silently dropped** — it is quarantined for review. Handlers signal retryability by raising
  `TransientError` vs `PermanentError`.
- **Ordering:** per-`aggregate_id` order is preserved (dispatcher serializes per aggregate, ADR-0006).
  Handlers MUST NOT assume global ordering across aggregates.
- **Signing (ADR-0007):** the outbox envelope is signed on write and verified before dispatch;
  an invalid/missing signature quarantines the event. Never disable verification.
- **Event naming & versioning:** `<module>.<past-tense-fact>`; `event_version` is semver. **Payload
  evolution is additive/back-compatible within a major** (add optional fields; never remove/retype a
  field). A breaking change is a new event or a new major version — old consumers keep working.
- **Catalog:** a new event or subscription is added to the event catalog (event-driven §25) in the
  same change that introduces it in code.

**Examples.**
```python
# GOOD — publish inside the business transaction
await self._cases.add(case)
await self._outbox.publish(
    event_type="case.status_changed", aggregate_type="case",
    aggregate_id=case.id, payload=payload, correlation_id=corr, actor_type="user")
```
```python
# GOOD — consumer claims the inbox before doing anything
if not await inbox.try_claim(event.event_id, "notify_on_status_change"):
    return                                  # redelivery — already handled
await do_side_effect(event)
await inbox.mark_processed(event.event_id, "notify_on_status_change")
```

**Anti-patterns.** Side effect before the inbox claim; publishing outside the transaction; renaming
an `event_type`; removing a payload field; assuming exactly-once.

**Common mistakes.** Non-idempotent handler; forgetting to add the event to the catalog; a handler
that assumes two events for different aggregates arrive in order.

**Review checklist.**
- [ ] Publish is in the same transaction as the write; not from a router.
- [ ] Handler claims the inbox before any side effect and is idempotent.
- [ ] Event name past-tense + in catalog; payload change is back-compatible.
- [ ] Failure path retries → dead-letters; nothing dropped silently.

---

## 8. API standards

**Purpose.** A consistent, versioned, correctly-status-coded REST surface matching `api-design.md`.

**Rules.**
- **Resource-oriented URLs**, standard methods; no verbs in paths. All business routes under
  `/api/v1`. Health/`/metrics` are unversioned well-known paths.
- **Status codes:** `200` read, `201` created (with `Location`), `202` accepted (async), `204` no
  content, `400` malformed, `401` unauthenticated, `403` unauthorized, `404` not found, `409`
  conflict/idempotency-fingerprint mismatch, `422` validation, `429` rate-limited, `503` dependency
  down. Never `200` with an error body.
- **Response & error envelope:** every response uses the `api-design.md` envelope; errors are
  `{error:{code,message,details,correlation_id}}` with a stable machine `code` — no stack trace, no
  internal detail leak. Map exceptions via the central handler (§10), not per-route try/except.
- **Never return an ORM model** — always a Pydantic `...Response`.
- **Pagination:** cursor or bounded offset with a max page size; return pagination metadata.
  **Sorting/filtering:** a defined, whitelisted field grammar — never interpolate client input into
  SQL/ORDER BY.
- **Validation:** Pydantic v2 request schemas at the boundary; reject unknown fields where strictness
  matters; validate before any side effect.
- **Idempotency (ADR-0012):** unsafe, retryable mutations accept an `Idempotency-Key`; same
  key+fingerprint replays the stored response, same key+different fingerprint → `422`, and the key is
  persisted in the same transaction as the write.
- **OpenAPI is the contract:** the spec is generated from the code; the SDK derives from it; keep
  route models and the spec in sync (CI fails on drift).
- **Versioning:** backward-compatible within `v1`; a breaking change is `v2` with a deprecation
  window — never a silent contract change.
- **Human-in-the-loop:** review-status mutations are **never optimistic** on the server contract —
  the response reflects the server-confirmed disposition (PRD FR-7.3).

**Examples.**
```python
@router.post("/api/v1/cases", status_code=201, response_model=CaseResponse)
async def open_case(body: OpenCaseRequest, uow: CaseUoW = Depends(get_case_uow)) -> CaseResponse:
    async with uow:
        case = await CaseService(uow.cases, uow.outbox).open_case(body.title)
        await uow.commit()
    return CaseResponse.from_domain(case)   # DTO, not the ORM row
```

**Anti-patterns.** `200` + `{"error": ...}`; returning an ORM object; unbounded list endpoints;
`ORDER BY {user_input}`; per-route bespoke error shapes.

**Common mistakes.** Missing idempotency on a retryable POST; leaking a DB error message; forgetting
`response_model`; unversioned business route.

**Review checklist.**
- [ ] Correct status code; standard envelope; stable error `code`; no leakage.
- [ ] Response is a DTO; request validated by Pydantic.
- [ ] Pagination bounded; sort/filter whitelisted.
- [ ] Retryable mutation honors `Idempotency-Key`; route under `/api/v1`.

---

## 9. Security coding standards

**Purpose.** Fail-closed, least-privilege, evidence-safe code (security-architecture.md is
authoritative; it supersedes briefer statements elsewhere).

**Rules.**
- **Secret handling:** secrets are `SecretStr`, read only via config, unwrapped only at point of
  use; never logged, never in an exception, never in a response, never committed. `.env` is dev-only;
  production/classified use Vault + External Secrets Operator.
- **Hashing:** passwords with **argon2id** (tuned params); tokens stored as a hash (argon2id / keyed
  HMAC via KMS), never plaintext. Use `secrets`/`os.urandom` for anything security-sensitive —
  **never** `random`.
- **Crypto:** all signing/encryption/data-keys go through `platform.crypto` (ADR-0009). Never
  hand-roll crypto, never call a provider SDK directly from a module, never name an algorithm at the
  call site (the policy engine decides).
- **TLS:** terminates at the ingress; the app trusts `X-Forwarded-*` only from the known proxy.
  In-cluster mTLS is a post-extraction concern — do not assume it in Phase 1 code.
- **Authentication/sessions (ADR-0010):** opaque server-side sessions (not stateless JWT for
  authorization decisions), immediate revocation; the store keeps only the token hash.
- **Authorization:** deny-by-default; every endpoint declares its authz (`require_role` /
  `require_case_access`). Never weaken or bypass a check "temporarily" without an explicit, reviewed,
  labeled exception. **RBAC** for role gates, **ABAC** for case-membership (`case_members`).
- **PII:** classify PII fields; minimize collection; honor retention; **mask PII in logs** (§10). PII
  never becomes a metric label.
- **Injection & OWASP:** parameterized queries only; validate/normalize all input at the boundary;
  no untrusted content treated as instructions (prompt-injection defense for AI inputs, later waves);
  follow the OWASP ASVS controls the security architecture cites.
- **Evidence integrity:** any deletion/purge/retention path checks **legal hold first** and refuses
  if held; evidentiary tables are append-only (never `UPDATE`/`DELETE`).

**Examples.**
```python
# GOOD — secret stays wrapped; crypto via the facade; no algorithm named
signature = await kms.sign(KeyRef(KeyPurpose.EVIDENCE_ROOT), canonical_bytes)
```
```python
# BAD — leaking a secret and rolling your own token
log.info("connecting", token=settings.vault_token.get_secret_value())   # secret in logs
token = str(random.random())                                            # insecure randomness
```

**Anti-patterns.** Logging a `SecretStr` value; `random` for tokens; a module calling a Vault/AWS SDK
directly; string-built SQL; a purge path that ignores legal hold; an endpoint with no authz.

**Common mistakes.** Forgetting to redact a nested secret; naming an algorithm at the call site;
returning a stack trace; storing a token instead of its hash.

**Review checklist.**
- [ ] No secret in logs/exceptions/responses; secrets via config only.
- [ ] Crypto only through `platform.crypto`; no algorithm named at call sites.
- [ ] Parameterized queries; input validated; authz declared and enforced.
- [ ] PII masked; deletion/purge checks legal hold; evidentiary tables append-only.

---

## 10. Logging standards

**Purpose.** One structured, correlated, PII-safe telemetry stream, strictly separate from the
evidentiary audit log.

**Rules.**
- **structlog** only; JSON renderer in every real environment; **no `print`**, no stdlib `logging`
  ad hoc.
- **Correlation & trace ids** are bound once in the HTTP middleware (`request_id`, `correlation_id`,
  `trace_id` from W3C `traceparent`) via contextvars — never threaded through call sites, never
  re-minted mid-request.
- **Audit separation (mandatory):** operational logs answer "what is the system doing" (Loki, ops
  retention/access). The **evidentiary audit log** answers "what legally happened to this evidence"
  (per-module tables, hash-chained per ADR-0003) — a *different system*. Never write audit facts to
  structlog; never write ops logs to audit tables.
- **Log levels:** `DEBUG` (dev only), `INFO` (lifecycle, request start/complete), `WARNING`
  (degraded/retryable), `ERROR` (handled failure), `EXCEPTION` (unhandled, with stack).
- **PII masking:** sensitive keys (`password`, `token`, `secret`, `authorization`, `api_key`,
  `set-cookie`) and `SecretStr` are redacted by a processor before render; don't log raw request
  bodies that may contain PII/evidence content.
- Log **events, not sentences**: `log.info("request_completed", status_code=200, duration_ms=12.3)`,
  not an interpolated string.

**Examples.**
```python
log.info("event_dispatched", event_type=event.event_type, aggregate_id=str(event.aggregate_id))
# BAD
print(f"processed {user.email} token={token}")     # print + PII + secret
```

**Anti-patterns.** `print`; interpolated log messages; logging a full request body; audit written to
structlog; re-minting correlation ids.

**Common mistakes.** A new sensitive key not in the mask list; logging a `SecretStr` (log its
presence, never its value); wrong level (ERROR for an expected 404).

**Review checklist.**
- [ ] structlog, JSON, event-style; no `print`.
- [ ] Correlation/trace ids present and not re-threaded.
- [ ] No PII/secret in logs; masking covers new sensitive keys.
- [ ] Audit facts go to the audit log, not telemetry.

---

## 11. Testing standards

**Purpose.** Make the quality bar executable; prove invariants, not just lines.

**Rules.**
- **Layers:** *unit* (pure logic, fakes), *integration* (real deps via Testcontainers), *contract*
  (same suite run against every adapter of a port), *architecture* (`import-linter` + boundary/SQL/
  secret asserts), *performance* (benchmark regressions for Tier-0/1).
- **Coverage floors (governance §4):** Tier-0 (`platform`, crypto, evidentiary/auth) **≥ 90%** *and*
  100% of security/evidentiary invariants explicitly tested; Tier-1 ≥ 80%; Tier-2 ≥ 60%. Coverage is
  a floor — an invariant test is worth more than a line.
- **Fixtures:** app via `asgi-lifespan`; a Settings-override per profile; DB session/UoW fixture with
  rollback isolation; ephemeral KMS dev keystore (tmp). No test touches a shared/real environment.
- **Factories** (`factory-boy`) build valid aggregates/DTOs; **test data is never real PII**.
- **Fake providers** (in-memory `ObjectStorage`/`Cache`/`RateLimiter`/`CryptoProvider`) for unit
  tests; real adapters covered by integration + contract tests. Fakes must satisfy the *same*
  contract suite as real adapters.
- **Testcontainers** for Postgres/Redis/MinIO/Vault; a missing container **skips with a clear
  message** (never a false pass); unit + architecture tests always run.
- **Determinism:** no sleeps-as-sync, no wall-clock assertions, no network to the internet. Async
  tests use `pytest-asyncio` (`asyncio_mode=auto`).
- **What every Tier-0 change must test:** the happy path, each failure/permission path, and each
  invariant (append-only, idempotency, downgrade/tamper for crypto, legal-hold refusal).

**Examples.**
```python
async def test_close_rejects_non_open_case() -> None:
    case = CaseFactory(status=CaseStatus.ARCHIVED)
    with pytest.raises(ConflictError):
        case.close()                     # invariant proven, not just coverage
```

**Anti-patterns.** Mocking the thing under test; asserting on log strings; a test that needs the
internet; coverage padding with no assertions; real PII in fixtures.

**Common mistakes.** Forgetting the redelivery (idempotency) test for a handler; not running the
contract suite against a new adapter; flaky wall-clock assertion.

**Review checklist.**
- [ ] Unit + integration (+ contract for a new adapter) + architecture tests present.
- [ ] Coverage floor met; invariants explicitly tested.
- [ ] Deterministic; no internet; missing containers skip, not pass.
- [ ] No real PII in test data.

---

## 12. Documentation standards

**Purpose.** Keep docs and code in lockstep; make intent legible to the 2039 maintainer.

**Rules.**
- **Docstrings** on every public module/class/function: what it does and its contract (pre/post
  conditions, raised errors), not a restatement of the code. Reference the governing doc/ADR where
  relevant (e.g. "guide Part 6", "ADR-0005").
- **Comments** explain *why*, never *what*; delete commented-out code; no `TODO`/`FIXME` in code
  presented as done — state incomplete work as incomplete in the PR, not hidden in a stub.
- **README** per module: purpose, public interface, events published/consumed, extraction note.
- **Architecture decisions:** an architecturally significant change gets/updates an ADR (governance
  §2); code that changes a documented behavior updates the authoritative doc **in the same change**
  (e.g. an endpoint shape change updates `api-design.md`; an event change updates the catalog).
- **Examples** in docs are illustrative and must stay compilable-in-spirit (match the frozen stack).

**Anti-patterns.** Docstring that repeats the signature; stale README; a behavior change without the
doc update; `TODO: fix later` in merged code.

**Common mistakes.** Adding an event in code but not the catalog; changing an error code without
touching `api-design.md`; leaving a stub function with a docstring claiming completeness.

**Review checklist.**
- [ ] Public API documented with its contract; comments explain why.
- [ ] Authoritative doc/ADR/catalog updated in the same PR.
- [ ] No TODO/stub-as-done; no commented-out code.

---

## 13. Git standards

**Purpose.** A clean, reviewable, revertible history.

**Rules.**
- **Branching:** trunk-based off `main`; short-lived `feat/<slug>` / `fix/<slug>` branches; no direct
  pushes to `main`; never commit or push unless explicitly asked.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`,
  `perf:`); imperative subject ≤ 72 chars; body explains *why*; footer references the ADR/task.
  AI-assisted commits carry the required co-author trailer per repo policy. Prefer a new commit over
  amending a shared one.
- **PRs:** small and focused; description links the frozen doc/ADR/task; fills the PR template
  checklist; green CI required; at least one approving reviewer (**two** for security/evidence/crypto
  or any Tier-0 change).
- **Review:** reviewers apply §16; a boundary/security/evidence violation is a blocking change, not a
  nit. Approvals don't carry across force-pushes that change scope.
- **Merge rules:** squash-merge to keep `main` linear; the squash message is a proper Conventional
  Commit; never merge red CI; never bypass hooks (`--no-verify`) or signing unless explicitly
  authorized.
- **Release tags:** semver tags on `main`; every release ships a validated rollback (governance
  deployment rule 7); tag notes reference the shipped ADRs/waves.

**Anti-patterns.** Giant mixed-purpose PRs; "wip" commit spam merged to main; force-push over
review; merging with a failing gate; skipping hooks.

**Common mistakes.** Non-conventional subject; PR that touches code but not the doc it changes;
amending a pushed commit others based work on.

**Review checklist.**
- [ ] Focused branch/PR; Conventional Commits; links the ADR/task.
- [ ] Green CI; correct number of reviewers; template checklist complete.
- [ ] Squash message well-formed; rollback validated for a release.

---

## 14. CI/CD standards

**Purpose.** The pipeline is the enforcement mechanism — a change that would fail CI is not done
(governance §11).

**Rules (gate order, all blocking unless noted).**
1. `ruff format --check` → 2. `ruff check` → 3. `mypy --strict` →
4. `import-linter` (both contracts) + `tests/architecture` →
5. `pytest -m "unit or architecture"` + coverage floor →
6. `pytest -m integration` with service containers →
7. security scan (SAST + secret scan; advisory→gating as it matures) →
8. dependency scan + **CycloneDX SBOM** artifact →
9. container build + CVE scan + **cosign sign** (approved base only) →
10. migration round-trip (`upgrade head` → `downgrade base`) →
11. deploy gates (release): GitOps/ArgoCD only, PreSync migrations in module-DAG order, validated
    rollback exercised in staging.
- **pre-commit** mirrors gates 1–4 + detect-secrets locally.
- No gate is skipped for a "quick fix." A bypass needs a labeled, time-boxed, board-approved
  exception recorded in the risk register.

**Anti-patterns.** Disabling a failing check to merge; committing without pre-commit; unsigned image;
`type: ignore`-ing mypy into silence.

**Common mistakes.** New code drops coverage below the floor; missing migration downgrade breaks gate
10; SBOM/scan skipped on a release.

**Review checklist.**
- [ ] All gates green; coverage floor held; SBOM + signed image produced.
- [ ] No skipped/bypassed gate without a recorded exception.
- [ ] Migration round-trip passes.

---

## 15. Performance standards

**Purpose.** Meet NFR budgets and scale by extension, not rewrite (system-design §10).

**Rules.**
- **Async end to end:** no blocking I/O on the request/job path; `asyncpg`, `httpx.AsyncClient`,
  `asyncio.sleep`. CPU-bound work goes to the worker.
- **No N+1:** eager-load with `selectinload`/`joinedload`; batch queries; an architecture/integration
  test guards hot read paths against per-row lazy loads.
- **Batching:** bulk-insert/bulk-update where semantics allow; the dispatcher drains the outbox in
  bounded batches; per-item-commit batches use explicit savepoints (ADR-0005).
- **Streaming:** large objects (evidence blobs, later) stream through the `ObjectStorage` port —
  never buffer a whole object in memory; server-side hashing streams (ADR-0008).
- **Memory:** bounded page sizes, generators/async iterators for large result sets; avoid loading a
  full table.
- **Connection pooling:** one async engine pool per process (`pool_pre_ping`, sized from config);
  HPA `min/maxReplicas` respect Postgres `max_connections` (governance deployment rule 8); Redis uses
  one shared pool.
- **Profiling & budgets:** Tier-0/1 changes carry benchmark regression tests; latency/throughput
  budgets are asserted, not assumed; RED/USE metrics expose regressions.

**Examples.**
```python
# GOOD — one query loads the relationship set
stmt = select(Case).options(selectinload(Case.findings)).where(Case.id == case_id)
```
```python
# BAD — a query inside a loop (N+1)
for cid in ids:
    await repo.get_case(cid)             # batch this instead
```

**Anti-patterns.** Blocking calls in async; N+1; buffering large blobs; unbounded lists; a new pool
per request.

**Common mistakes.** Lazy relationship access; forgetting a benchmark on a hot path; oversized HPA
bounds exhausting the DB pool.

**Review checklist.**
- [ ] Async, no blocking I/O; CPU-bound work offloaded to the worker.
- [ ] No N+1; relationships eager-loaded; large I/O streamed.
- [ ] Pooling correct; page sizes bounded; Tier-0/1 has a benchmark.

---

## 16. Code review checklist (mandatory)

Reviewers apply this to every PR. A checked box means *verified*, not *assumed*. Boundary, security,
and evidence items are **blocking**, not nits.

**Correctness & design**
- [ ] Invariants live in aggregates; illegal states unrepresentable (no anemic model).
- [ ] Router is thin; application service orchestrates; repository persists only.
- [ ] Response is a DTO; no ORM model crosses the API or a module boundary.

**Boundaries & imports**
- [ ] Import DAG respected; `import-linter` green; `platform` imports no module.
- [ ] Cross-module reads via `public.py`; cross-module writes event-mediated (INV-5).
- [ ] Schema set on new models; no cross-schema FK.

**Transactions & events**
- [ ] UoW opened/committed at the entrypoint; service never commits.
- [ ] Outbox publish is in the business transaction; handler claims inbox first and is idempotent.
- [ ] Event name past-tense + in catalog; payload change back-compatible; retry→dead-letter path
      intact.

**Security & evidence**
- [ ] No secret in logs/exceptions/responses; crypto only via `platform.crypto`.
- [ ] Parameterized queries; input validated; authz declared and enforced (RBAC/ABAC).
- [ ] PII masked; deletion/purge checks legal hold; evidentiary tables append-only.

**Quality & tests**
- [ ] mypy strict clean; naming conventions followed; no TODO/stub-as-done.
- [ ] Unit + integration (+ contract for new adapters) + architecture tests; coverage floor met;
      invariants tested.
- [ ] Docs/ADR/catalog updated in the same PR; correct log levels; no `print`.

**Ops**
- [ ] Migration has a tested `downgrade()`; indexes justified.
- [ ] Metrics/logs emitted with the right names; `/readyz` unaffected or updated.
- [ ] CI fully green; SBOM/signed image for a release.

Reviewers with insufficient context on a security/evidence/crypto change escalate to the relevant
board (governance §12) rather than approving.

---

## 17. Definition of Done

Four nested DoDs. Each level includes the ones below it.

**Engineering DoD (a change/task):**
- [ ] Implements the frozen spec; no redesign, no new tech, no new ADR (or a blocker was raised as
      one).
- [ ] Code conforms to §§1–15; mypy strict + ruff + import-linter green.
- [ ] Tests written and green; coverage floor met; invariants tested; no TODO/stub-as-done.
- [ ] Docs/ADR/catalog updated in the same change; secrets clean; logs structured + PII-safe.

**PR DoD:** Engineering DoD **plus** — small/focused; Conventional Commits; links the ADR/task; PR
template checklist complete; §16 review passed; correct reviewer count (2 for Tier-0); full CI green.

**Module DoD:** all its PRs' DoD **plus** — public interface documented (README, published/consumed
events in the catalog); boundary enforced by architecture tests; module-DAG position correct;
Tier-appropriate coverage and benchmarks; a threat model for any Tier-0/1 surface (governance §5);
runbook impact assessed.

**Release DoD:** Module DoDs **plus** — all governance §13 items; SBOM produced + image cosign-signed;
migrations PreSync/DAG-ordered; **validated rollback exercised in staging**; observability
(dashboards/alerts) in place; Production Readiness Review passed (governance §12). A release is a
claim of fact — if a gate was skipped, it is not done.

---

*This manual is versioned by supersession under governance §"Amending this constitution": changing a
standard requires an ADR + ARB (and the relevant board for security/forensics/AI). It never
overrides the frozen architecture — it operationalizes it. When the frozen docs change via ADR,
update the affected standard here in the same change.*
