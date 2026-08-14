# SentinelAI — Implementation Wave 1: Platform Foundation

**Status:** Execution manual (engineering-authoritative for Wave 1). **Architecture is FROZEN
(v1.0).** This document *converts* the approved architecture into an ordered, testable build plan.
It introduces **no new design** and **no new ADR** — it only references the frozen corpus. If a true
implementation blocker is found, raise it as an ADR per `engineering-governance.md` §2; otherwise
build to the docs as written.

**References (immutable):** `architecture.md`, `system-design.md`, `database-design.md`,
`api-design.md`, `security-architecture.md`, `deployment-architecture.md`,
`event-driven-architecture.md`, `canonical-evidence-model.md`, `backend-implementation-guide.md`
(the *how* authority — cited as "guide PartN"), `engineering-governance.md`, `engineering-roadmap.md`,
ADR-0002/0004/0005/0006/0007/0009/0010/0012, and `architecture-validation-report.md`.

---

## 0. Wave 1 scope & non-goals

**In scope — platform foundation only (`apps/server/src/sentinelai/platform` + `entrypoints` +
project tooling):** repository governance, configuration, dependency injection/composition root,
logging, metrics, health, error handling, database foundation, Redis foundation, object-storage
**port** (no evidence flow), event infrastructure (outbox/inbox/dispatcher/envelope), security
**primitives** (hashing, secure random, secret loading, TLS/cert assumptions), testing foundation,
Docker dev stack, CI/CD, code-quality gates.

**Explicitly OUT of scope (later waves):** every business module (`modules/*`), evidence ingestion &
storage flow, cases, investigation/AI, the full ADR-0010 session/RBAC/ABAC login model (only the
crypto *primitives* land here), CQRS projections, search, notifications delivery. The `modules/*`
tree may already contain skeleton code from earlier exploratory work; **Wave 1 does not build on it
and does not certify it** — it is governed by its own future wave.

**Two frozen invariants from the validation report that Wave 1 must bake into the foundation:**
- **INV-5 (boundary):** cross-module public-interface calls are **read-only**; all cross-module
  state change is event-mediated. Enforced by the import-linter contracts + the dispatcher being the
  only cross-module write path. Wave 1 owns that enforcement.
- **INV-1 (evidentiary reads):** evidentiary/court reads come only from the write model, never a
  projection/cache. No projection exists in Wave 1, so Wave 1's job is to ensure **no
  general-purpose cache (Redis) is ever positioned as a system of record** — a caching-policy rule
  (§12).

---

## 1. Wave 1 Definition of Done (the exit bar)

Wave 1 is **done** when all hold (this is the universal DoD of `engineering-governance.md` §13,
specialized to the foundation):

1. `apps/server` starts (`http` + `worker`) against the Docker dev stack; `/healthz` 200 always,
   `/readyz` 200 only when Postgres+Redis+KMS+object-store reachable, 503 (with the failing check
   named) otherwise.
2. `alembic upgrade head` and `alembic downgrade base` both succeed on a clean database; every
   migration has a real, tested `downgrade()`.
3. The full CI pipeline (§18) is green: `ruff` (lint+format), `mypy --strict`, `import-linter`
   (both contracts), `pytest` with coverage ≥ **90%** on `platform` (Tier-0 floor), architecture
   tests, security scan, SBOM, signed container build.
4. Every foundation component below has: unit tests for its logic, an integration test against a
   real dependency (Testcontainers) where it touches one, and an architecture test asserting its
   import boundaries.
5. Configuration validates at startup for all five profiles (§5); a missing/placeholder secret
   fails closed in `production`/`classified`.
6. No `TODO`/`FIXME`/stub-as-done in `platform`; no secret in the repo; structured logs only.
7. This document's §20 task list is fully checked off and each task's acceptance criteria met.

---

## 2. Current-state reconciliation (build vs. harden)

The platform skeleton **already exists**. Wave 1 is mostly *hardening to the exit bar*, not
greenfield. Status legend: **✅ exists** · **◑ partial (harden)** · **▧ to build**.

| Component | Path | Status | Wave-1 work |
|---|---|---|---|
| Config / Settings | `platform/config.py` | ◑ | add profile validation, startup validation, classified/air-gapped profiles, reconcile legacy JWT fields (§5) |
| Logging | `platform/logging.py` | ◑ | add PII masking processor + audit-vs-telemetry note (§7) |
| DB engine/session/UoW/base | `platform/db/*` | ✅ | add per-role engines (ADR-0004), repository base class (§11) |
| Alembic | `platform/migrations/*` + `alembic.ini` | ◑ | role-grant migration (ADR-0004), downgrade tests, naming lint |
| Outbox / Inbox / Envelope | `platform/events/{outbox,inbox,envelope}.py` | ✅ | signed envelope hook (ADR-0007), serializer module (§14) |
| Dispatcher | `platform/events/dispatcher.py` | ◑ | relocate out-of-process + `SKIP LOCKED` + per-aggregate order (ADR-0006) (§14) |
| KMS / crypto | `platform/crypto/*` | ✅ (arch-approved) | execution validation only (its own gate — not re-opened here) |
| HTTP entrypoint | `entrypoints/http/*` | ◑ | metrics registry, startup validation, DI composition (§6, §8) |
| Worker entrypoint | `entrypoints/worker/*` | ◑ | host the relocated dispatcher (ADR-0006), scheduler hook |
| Health | `entrypoints/http/health.py` | ◑ | add object-store check, startup probe, cached readiness (§9) |
| Middleware | `entrypoints/http/middleware.py` | ✅ | add W3C `traceparent` propagation (§7) |
| Exception handlers | `entrypoints/http/exception_handlers.py` | ◑ | wire to the exception hierarchy + API envelope (§10) |
| Object storage port | `platform/storage/*` | ▧ | build the `ObjectStorage` port + MinIO adapter (§13) |
| Redis foundation | `platform/cache/*` | ▧ | build cache/rate-limit/connection abstractions (§12) |
| Security primitives | `platform/security/*` | ▧ | argon2id hasher, secure-random/token, secret loader, TLS/cert (§15) |
| Metrics registry | `platform/observability/*` | ▧ | RED/USE/platform metric helpers + naming (§8) |
| Error hierarchy + HTTP mapping | `shared/exceptions.py` + `entrypoints/http/exception_handlers.py` | ✅ | **DONE** — guide Part 11 taxonomy + envelope handler already exist; do not duplicate under `platform/` (§9) |
| Testing foundation | `tests/*`, `conftest.py` | ◑ | Testcontainers, fake providers, contract + architecture tests (§16) |
| Docker dev stack | `docker-compose.dev.yml`, `Dockerfile` | ◑ | add MinIO/Vault(dev), volumes, network, healthchecks (§17) |
| CI/CD | `.github/workflows/*` | ▧ | full pipeline (§18) |

---

## 3. Repository & package governance

**Layout (authoritative for `apps/server`):**
```
apps/server/
  pyproject.toml            # deps + ruff + mypy + pytest + import-linter (single tool config)
  alembic.ini               # migration runner config
  Dockerfile  Makefile  README.md  docker-compose.dev.yml
  scripts/                  # operational scripts (kms bootstrap, etc.) — no business logic
  src/sentinelai/
    __init__.py             # __version__
    shared/                 # lowest layer: pure types/utils, ZERO deps on platform or modules
    platform/               # cross-cutting plumbing (this wave). Imports shared only.
      config.py  logging.py  errors.py
      db/  cache/  storage/  events/  crypto/  security/  observability/  migrations/
    modules/                # business modules (OUT of Wave 1)
    entrypoints/
      http/                 # FastAPI composition root (thin)
      worker/               # arq worker composition root (thin) — hosts dispatcher (ADR-0006)
  tests/
    unit/  integration/  contract/  performance/  architecture/  fixtures/  factories/
    conftest.py
```

**Package ownership** (per `architecture.md` "Team Ownership" → "Platform & Infrastructure" squad
owns everything in this wave): `platform/*`, `entrypoints/*`, `infra/`, CI, `docker-compose`,
tooling config. ADRs & this doc are guild-owned.

**Dependency & import rules (enforced by `import-linter`, already configured in `pyproject.toml`):**
- Layered DAG (highest→lowest): `entrypoints` → `modules.*` (investigation above domain modules) →
  `platform` → `shared`. A layer imports only **lower** layers.
- **`platform` may never import `modules`** (the `platform is domain-agnostic` forbidden contract).
- No relative cross-module imports (ruff TID251/TID252); absolute imports only.
- Cross-module code goes through a module's `public.py` or the event bus — never `models.py`/
  `repository.py`. (Foundation relevance: the dispatcher and outbox are the *only* sanctioned
  cross-module write channel — INV-5.)

**Naming conventions:** `snake_case` modules/functions, `PascalCase` classes, `UPPER_SNAKE`
constants; Pydantic schemas suffixed `...Request`/`...Response`; ports named for the capability
(`ObjectStorage`, `PasswordHasher`, `CryptoProvider`), adapters named for the tech (`MinioObjectStorage`,
`Argon2PasswordHasher`); DB constraint/index names from the `NAMING_CONVENTION` in `db/base.py`.

---

## 4. Configuration

**Purpose.** One typed, validated source of settings for every profile.
**Responsibilities.** Load → type-coerce → validate → expose immutable settings; never read raw
`os.environ` outside this module.
**Folder location.** `platform/config.py` (single `Settings(BaseSettings)`).
**Interfaces.** `Settings`, `get_settings()` (lru-cached), module-level `settings`, `tenant_id`
ContextVar (reserved single-tenant, always `None` until the Phase-4 multi-tenancy ADR — do not
populate).
**Dependencies.** `pydantic-settings`. **Startup order.** Constructed first, before logging/DB/KMS.
**Failure modes.** Invalid/missing required value → raise at construction (fail closed). **Testing.**
Unit: each profile validates; production rejects placeholder secrets. **Future extensibility.** New
provider config = new typed fields; multi-tenancy activates `tenant_id` without shape change.

**Configuration hierarchy (highest precedence first):**
1. Process environment variables (UPPER_SNAKE, mapped to `Settings` fields).
2. Secrets injected by Vault → External Secrets Operator as env vars (production/classified).
3. `.env` file (local dev only; git-ignored; `.env.example` holds placeholders).
4. Field defaults in `Settings` (dev-safe only; never a real secret).

**Environment variables (foundation subset — full list in `.env.example`):** `APP_ENV`,
`LOG_LEVEL`, `API_HOST/PORT`; `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`; `REDIS_URL`;
`STORAGE_ENDPOINT_URL`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`;
`KMS_PROVIDER` + KMS/Vault vars (ADR-0009). All secrets are `SecretStr`.

**Secrets.** Never committed; never logged (redacted `SecretStr`). Production/classified receive
them only via Vault+ESO. `.env` is a dev convenience only.

**Profiles (`APP_ENV`):**
| Profile | KMS | Object store | Egress | Secrets | Notes |
|---|---|---|---|---|---|
| `development` | `dev` software keystore | MinIO (compose) | open | `.env` placeholders | console logs; auto-reload |
| `testing` | `dev` (ephemeral) | MinIO (Testcontainer) | open | env fixtures | JSON logs; deterministic; no external calls |
| `production` | `vault_transit`/HSM | S3/MinIO | controlled | Vault+ESO | fail closed on placeholder secret/KMS |
| `air-gapped` | `vault_transit`/HSM (in-enclave) | MinIO in-enclave | **zero** | Vault+ESO in-enclave | no external DNS/anchoring path — offline trusted-time source required |
| `classified` | HSM (PKCS#11) | on-prem WORM | **zero** | Vault+ESO in-enclave | strictest; production rules + no `dev` provider, no debug endpoints |

`air-gapped`/`classified` are hardening overlays on `production` (all production fail-closed rules
apply, plus: no outbound path, no `dev` KMS, no `/docs` exposure to untrusted networks).

**Validation strategy.** Pydantic v2 field/model validators at construction. Add a
`Settings.validate_for_profile()` that asserts profile-specific invariants (e.g. `production` ⇒
`kms_provider != "dev"`, no placeholder `SecretStr`, `database_url` uses `+asyncpg`).
**Startup validation.** Both entrypoints call `settings.validate_for_profile()` **before** opening
any connection; failure aborts startup with a clear, secret-free error. `/readyz` never masks a
config failure — a mis-configured process must fail to start, not run degraded.

---

## 5. Dependency injection & composition root

**Purpose.** Wire concrete adapters to ports exactly once, at the edge; keep business/plumbing code
depending on **interfaces**, not constructors.
**Pattern (frozen by the guide).** FastAPI's `Depends` is the DI mechanism for request-scoped
wiring; the **composition roots** are `entrypoints/http/main.py:create_app()` and
`entrypoints/worker/main.py:WorkerSettings.on_startup`. Only the composition root may import concrete
adapters and modules (it is the top DAG layer). **No service-locator**: nothing calls a global
registry to *fetch* a dependency; dependencies are *injected* (constructor args or FastAPI
`Depends`). `app.state`/`ctx` hold only process-lifetime singletons created at startup.

- **Provider registration.** Ports (`CryptoProvider`, `ObjectStorage`, `PasswordHasher`, event
  consumers) are selected by config and constructed in the composition root: KMS via
  `create_kms(settings)`; object storage via a `build_object_storage(settings)` factory; event
  consumers via each module's `register_consumers(dispatcher)` (module wiring — not Wave 1, but the
  dispatcher registration API is Wave 1).
- **Lifetime management.** *Process-singleton*: engine, session factory, KMS, object-storage client,
  Redis pool, dispatcher, arq pool (built in lifespan, disposed in the `finally`). *Request-scoped*:
  `AsyncSession`/UoW (yielded per request via `get_session`/`get_unit_of_work`). *Transient*: value
  objects, DTOs. Never share an `AsyncSession` across requests.
- **Factories.** `create_kms`, `build_object_storage`, `build_password_hasher`, `create_app` — pure
  functions of `Settings`, so tests build isolated instances with fakes.
- **Interfaces (ports) delivered in Wave 1.** `CryptoProvider` (exists), `ObjectStorage`,
  `PasswordHasher`, `TokenGenerator`, `Cache`, `RateLimiter`, `AuditSink` (exists in crypto),
  `EventHandler`/`UowFactory` (exists).
**Startup order (composition root).** config → logging → DB engine → Redis pool → object-storage
client → KMS (`start()` + health, fail-closed) → dispatcher (register consumers, start) → arq pool
→ routers/metrics. **Shutdown reverses it**, draining the dispatcher first.
**Failure modes.** Any singleton that must exist (DB, KMS in prod) fails startup; best-effort ones
(arq pool) log a warning and surface on use. **Testing.** `asgi-lifespan` drives lifespan in tests;
`app.dependency_overrides` swaps ports for fakes. **Future extensibility.** New adapter = new factory
branch on config; composition root is the only edit site.

---

## 6. Logging

**Purpose.** One structured, correlated, PII-safe log stream for engineering telemetry — **distinct
from the evidentiary audit log**.
**Folder.** `platform/logging.py` (`configure_logging`, `log`).
**Responsibilities & interfaces.** structlog, JSON renderer in every real env (Loki), console only
in `development`. `correlation_id`/`request_id`/`trace_id` bound once in HTTP middleware via
`merge_contextvars` — no call site threads them.
- **Correlation & trace IDs.** `request_id` server-minted always; `correlation_id` from
  `X-Correlation-Id` or minted; **Wave-1 add:** parse/propagate W3C `traceparent` so `trace_id` is a
  real distributed-trace id from day one (guide Part 2 / system-design §12).
- **Audit separation (mandatory).** Operational logs answer "what is the system doing" and live in
  Loki with ops retention/access. The evidentiary **audit log** (per-module tables, hash-chained per
  ADR-0003) answers "what legally happened to this evidence" and is a *different system* — never
  write audit facts to structlog, never write ops logs to the audit tables (system-design §12,
  governance §5).
- **PII masking (Wave-1 add).** A structlog processor redacts known-sensitive keys
  (`password`, `token`, `secret`, `authorization`, `api_key`, `set-cookie`) and `SecretStr` before
  render. Value objects redact their `repr` (already true for crypto secrets).
- **Log levels.** `DEBUG` (dev only), `INFO` (lifecycle/request start-complete), `WARNING`
  (degraded/retryable), `ERROR` (handled failure), `EXCEPTION` (unhandled, with stack). No `print`.
**Dependencies.** structlog. **Startup order.** Configured immediately after config.
**Failure modes.** Logging must never raise into request flow; renderer errors degrade to a minimal
line. **Testing.** Capture-log fixture asserts fields present + secrets masked. **Future
extensibility.** OpenTelemetry log export slots in as an extra processor; trace-context format is
already OTel-compatible.

---

## 7. Metrics

**Purpose.** Prometheus instrumentation with a consistent taxonomy that survives module extraction.
**Folder.** `platform/observability/metrics.py` (helpers) + `/metrics` exposed in `create_app`
(exists via `prometheus-fastapi-instrumentator`). Crypto already ships `platform/crypto/metrics.py`.
**Responsibilities & interfaces.** Central registry helpers so every metric follows the naming rule;
`/metrics` is network-restricted (api-design §12), `include_in_schema=False`.
- **RED** (request-like: HTTP, module public interfaces, dispatcher handlers): `*_requests_total`,
  `*_request_errors_total`, `*_request_duration_seconds` (histogram).
- **USE** (platform resources: Postgres pool, Redis, object store): `*_utilization`,
  `*_saturation`, `*_errors_total`.
- **Platform metrics:** DB pool in-use/overflow, Redis latency, dispatcher lag
  (`event_dispatch_pending`), outbox `dead_letter` count, KMS latency/breaker (exist).
- **Business metrics:** *none in Wave 1* (no business modules); the naming slot is reserved:
  `sentinelai_<module>_<fact>_total`.
- **Naming convention:** `sentinelai_<subsystem>_<metric>_<unit>`; labels low-cardinality only
  (`method`, `route_template`, `outcome`, `provider`) — **never** an id/PII as a label.
**Dependencies.** `prometheus-client`, instrumentator. **Startup order.** Registry at import; `/metrics`
mounted in `create_app`. **Failure modes.** Metric emission never blocks request flow. **Testing.**
Assert metric names/labels render; cardinality guard test. **Future extensibility.** Same names map
1:1 to per-service dashboards after extraction (system-design §12).

---

## 8. Health checks

**Purpose.** Correct liveness/readiness/startup semantics for compose today and Kubernetes probes
later.
**Folder.** `entrypoints/http/health.py` (exists).
**Interfaces & responsibilities.**
- **Liveness `/healthz`** — process up; checks **no** dependencies (a dependency outage must fail
  *readiness* and reroute, not restart a healthy pod).
- **Readiness `/readyz`** — every request-path dependency reachable, else `503` naming the failing
  check. Wave-1 add: **object store** check alongside postgres/redis/kms.
- **Startup probe (Wave-1 add)** — a `/startupz` (or reuse `/readyz` with a startup gate) that stays
  failing until first successful config-validate + migrations-current + KMS-started, so K8s startup
  probe holds traffic until the process is truly ready.
- **Dependency health:** Postgres `SELECT 1`, Redis `PING`, KMS `health()` (READY/DEGRADED/
  UNAVAILABLE → 503 unless READY), object store `HeadBucket`.
- **Caching:** readiness checks are cheap but run per-poll; **Wave-1 add** a short TTL (e.g. 2–5 s)
  in-process cache of each check result to bound probe load under frequent scraping — never cache a
  *failure* longer than the TTL.
- **Failure behavior:** readiness **reports**, never raises; all checks run so the response lists
  every failing dependency, not just the first.
**Dependencies.** db engine, Redis, KMS, object store. **Startup order.** Router mounted in
`create_app`; checks depend on singletons being constructed. **Testing.** Integration: kill each
dependency → `/readyz` 503 with that check failing; `/healthz` stays 200. **Future extensibility.**
Add a dependency = add one check function; K8s probe config unchanged.

---

## 9. Error handling

**Purpose.** One domain-exception taxonomy mapped deterministically to the API error envelope.
> **Correction (2026-07-28):** the domain-exception hierarchy already exists at
> **`sentinelai/shared/exceptions.py`** — this is the location **guide Part 11 mandates**, and it is
> used by all 8 modules + the HTTP handler (38 files). Do **not** create a second taxonomy under
> `platform/` (an earlier attempt at `platform/errors.py` was a duplicate and has been removed). Each
> class there carries `code` + `http_status` + `details`, 1:1 with api-design.md §2.4. The HTTP
> mapping in `entrypoints/http/exception_handlers.py` is **already implemented** — this component is
> DONE, not a build task.
**Folder.** `shared/exceptions.py` (domain taxonomy, guide Part 11 — **exists**) +
`entrypoints/http/exception_handlers.py` (envelope mapping — **exists**). Crypto keeps its own
infrastructure `CryptoError` subtree (`platform/crypto/exceptions.py`); config keeps
`ConfigurationError` (a startup fail-closed error). Neither is an HTTP domain error.
**Exception hierarchy (existing, guide Part 11):**
```
DomainError                        # base; code="INTERNAL_ERROR", http_status=500
├── ValidationFailedError          # VALIDATION_FAILED / 422
├── EvidenceImmutableError         # EVIDENCE_IMMUTABLE / 409
├── LegalHoldViolationError        # LEGAL_HOLD_VIOLATION / 409
├── NotFoundError                  # NOT_FOUND / 404
├── ForbiddenError                 # FORBIDDEN / 403
├── UnauthenticatedError           # UNAUTHENTICATED / 401
├── ConflictError                  # CONFLICT / 409
└── PreconditionFailedError        # PRECONDITION_FAILED / 412
# infrastructure errors are NOT here: platform/crypto/exceptions.py (CryptoError → 503/500),
# config.ConfigurationError (startup fail-closed). They fall through to the 500/503 handler path.
```
- **Domain vs infrastructure.** Domain errors (`shared/exceptions`) are expected, caller-facing,
  mapped to 4xx by their `http_status`. Infrastructure errors are operational, mapped to 5xx.
- **API mapping (exists).** `domain_error_handler` maps any `DomainError` → the api-design.md §2.4
  envelope (`{error:{code,message,details,request_id,correlation_id,timestamp}}`) with `http_status`;
  `RequestValidationError` → 400 (malformed shape vs 422 domain rule); unmapped → `500` (no leak).
- **Retryable vs permanent.** KMS `KmsUnavailable`/timeouts are retried inside the KMS resilience
  layer and by the dispatcher's attempt/backoff→dead-letter path; the dispatcher dead-letters after
  `max_attempts`.
**Remaining (optional, verify-first).** `exception_handlers.py` maps `KmsUnavailable` to a generic
500; api-design.md §2.4 has `SERVICE_UNAVAILABLE`/503 for a dependency being down — a small
`CryptoError`→503 handler would be more accurate. Confirm against api-design before adding.
**Testing.** Existing module tests exercise the domain errors; the handler mapping is covered by the
integration API tests.

---

## 10. Database foundation

**Purpose.** Async Postgres access with schema-per-module ownership, append-only role separation
(ADR-0004), and the transaction boundary at the entrypoint (ADR-0005).
**Folder.** `platform/db/{base,session,uow}.py` (exist), `platform/migrations/*`, `alembic.ini`.
**SQLAlchemy.** 2.0 async, `asyncpg`; `Base` with deterministic `NAMING_CONVENTION`; each model sets
`__table_args__={"schema": "<module>"}`; **no cross-schema `ForeignKey`** — inter-schema refs are
plain `UUID` columns validated in the app layer (database-design §5). `expire_on_commit=False`,
`autoflush=False`; relationships eager-loaded explicitly (`selectinload`), never lazy.
**Connection management & pooling.** One async engine (`pool_pre_ping=True`, `pool_size`,
`max_overflow` from config). **Wave-1 add (ADR-0004):** **role separation** — distinct connection
URLs/roles: `sentinel_migrator` (DDL; Alembic PreSync only), `sentinel_app` (DML on mutable tables),
`sentinel_append` (INSERT+SELECT on evidentiary tables). The runtime engine uses `sentinel_app`
(+`sentinel_append`) — never DDL/`UPDATE`/`DELETE` on evidentiary tables. HPA bounds must respect
Postgres `max_connections` (deployment §; governance deployment rule 8).
**Transactions.** UoW opened/committed **at the entrypoint boundary** (HTTP dependency / worker job
wrapper), one transaction per request/use-case; services never `commit()`/`rollback()`; **outbox
write is in the same transaction** (event-driven §16). Batch per-item commits use explicit savepoints
(ADR-0005). The generic `UnitOfWork` is subclassed per module (later waves) to attach repositories +
`OutboxWriter`.
**Repository base class (Wave-1 build).** `platform/db/repository.py`: a generic async base
(`get/add/list` by typed model + session) that **persists only** — no business logic (governance §4,
guide Part 5). Modules subclass it; it never commits.
**Migration strategy (Alembic).** Async env (`platform/migrations/env.py`, exists); one linear
history; **module-DAG order** enforced at deploy as ArgoCD PreSync hooks (`platform` → ingestion →
domain → investigation → notification; deployment rule 5). Naming: `YYYYMMDDNNNN_<slug>.py`. **Every
migration has a real, tested `downgrade()`** (governance rule 9). The generic per-schema
`outbox_events`/`inbox_events` tables are created by hand-written migrations (not autogenerate),
using the Core tables from `events/outbox.py`.
**Startup order.** Engine built after config; migrations run out-of-band (PreSync), **not** at app
startup. **Failure modes.** Unreachable DB → `/readyz` 503; pool exhaustion surfaced as
`TransientError`. **Testing.** Testcontainers Postgres; `upgrade head`→`downgrade base` round-trip in
CI; architecture test asserts no cross-schema FK and no raw string SQL. **Future extensibility.**
Per-tenant schema (ADR-0014) and read replicas (CQRS, ADR-0013) attach without changing the base.

---

## 11. Redis foundation

**Purpose.** One managed Redis access layer for caching, the arq job queue backing, rate limiting,
and (later) session storage — with a strict "never a system of record" rule.
**Folder.** `platform/cache/*` (Wave-1 build): `client.py` (pool), `cache.py` (`Cache` port),
`ratelimit.py` (`RateLimiter` port).
**Responsibilities & interfaces.**
- **Caching.** A `Cache` port (`get/set/delete` with TTL, namespaced keys `sai:<subsystem>:<key>`).
  **INV-1 rule:** the cache is an accelerator only; **evidentiary/authoritative reads never come from
  it** — it may cache derived/ops data, never a source-of-truth evidence record.
- **Session storage.** Redis is the intended session store (ADR-0010) — **primitive only in Wave 1**
  (connection + key convention reserved); the session model itself is a later (auth) wave.
- **Rate limiting.** A `RateLimiter` port (fixed/sliding window via Redis counters) with per
  client-class policy stubs (api-design §; concrete policy is later, the mechanism is Wave 1).
- **Temporary queues.** arq uses Redis as the Phase-1 job queue (already wired); this is the
  execution mechanism only — durable event traffic is the outbox, not Redis (event-driven §).
- **Connection strategy.** One shared async connection pool per process (created at startup, closed
  at shutdown); `redis.asyncio`; health via `PING` (already in `/readyz`).
**Dependencies.** `redis`, `arq`. **Startup order.** Pool after DB, before dispatcher/arq.
**Failure modes.** Redis down → cache misses degrade to source (never error the request), rate limiter
**fails closed** for protected routes, arq enqueue surfaces `TransientError`. **Testing.**
Testcontainers Redis; cache hit/miss/expiry; rate-limit window; fail-closed behavior. **Future
extensibility.** Redis Cluster/Sentinel HA and per-tenant key prefixes attach without API change.

---

## 12. Object storage foundation

**Purpose.** A provider-neutral blob abstraction so evidence storage (later waves, ADR-0008) and
cloud/on-prem/air-gapped all use one code path. **Wave 1 delivers the port + adapter + bucket
bootstrap only — no evidence flow, no WORM/quarantine logic (that is ADR-0008, a later wave).**
**Folder.** `platform/storage/*` (Wave-1 build): `port.py` (`ObjectStorage` Protocol), `minio.py`
(`MinioObjectStorage` / S3 adapter via `httpx`/SDK), `factory.py` (`build_object_storage(settings)`).
**Interfaces (`ObjectStorage` port).** `put_stream`, `get_stream`, `head`, `create_multipart`,
`upload_part`, `complete_multipart`, `presign_put/get`, `ensure_bucket`. Streaming-first: objects are
never fully buffered in memory.
- **MinIO/S3.** Same S3 API for MinIO (dev/on-prem/air-gapped) and S3 (cloud) — identical adapter,
  endpoint from config.
- **Bucket strategy.** Wave-1 bootstraps only the buckets the foundation needs to prove the path
  (a `platform-scratch`/health bucket). The evidence bucket taxonomy (`quarantine` vs immutable
  `evidence`, Object-Lock/WORM) belongs to ADR-0008 and is **not** created in Wave 1.
- **Multipart uploads & streaming.** The port exposes multipart + presigned URLs so large-object
  handling exists at the foundation, exercised by a foundation integration test (round-trip a large
  stream), but wired to evidence only later.
**Dependencies.** MinIO client/`httpx`, KMS (later, for envelope encryption — not Wave 1). **Startup
order.** Client after config; `ensure_bucket` for the scratch bucket at startup; health via
`HeadBucket`. **Failure modes.** Unreachable store → `/readyz` 503; upload failure → `TransientError`.
**Testing.** Testcontainers MinIO; stream/multipart round-trip; presign; fake in-memory adapter for
unit tests. **Future extensibility.** ADR-0008 layers quarantine→scan→promote→WORM + envelope
encryption *behind the same port*; classified profile swaps to an on-prem WORM adapter with no caller
change.

---

## 13. Event infrastructure

**Purpose.** Reliable, at-least-once, idempotent, order-preserving event backbone whose transport can
swap to Redpanda later without touching modules.
**Folder.** `platform/events/{envelope,outbox,inbox,dispatcher}.py` (exist) + `serializer.py`
(Wave-1 build).
**Envelope model.** `EventEnvelope` (frozen, slots) = full `outbox_events` column set + the
correlation/causation/trace triad (event-driven §11). `from_row` builds it from a fetched mapping.
Immutable value object handed to every handler.
**Outbox base.** Per-schema `outbox_events` Core table (one factory, memoized per schema);
`OutboxWriter.publish` inserts one `pending` row **on the caller's open UoW session** — same
transaction as the business write (§16 atomicity). Wave-1 keeps this generic; modules use it later.
**Inbox base.** Per-schema `inbox_events`; `InboxGuard.try_claim` does insert-first on
`(event_id, handler_name)` inside a savepoint (IntegrityError → redelivery, skip); `mark_processed`
after side effects. Assumes at-least-once always.
**Dispatcher skeleton.** Exists as the Phase-1 in-process poller (per-schema drain, at-least-once,
handler-per-transaction, `pending`→`dispatched`/`dead_letter`, graceful drain). **Wave-1 hardening to
ADR-0006:** relocate the relay **out of the HTTP process into the worker** (or a dedicated dispatcher
deployment); switch drain to `SELECT ... FOR UPDATE SKIP LOCKED` for safe multi-replica competing
consumers; **preserve per-`aggregate_id` ordering** (advisory lock or hash-partition); gate retries
on `last_attempted_at` backoff. The registration API, envelope, outbox, inbox, and catalog are
**unchanged** — only the relay moves (this is the documented Phase-1→3 seam).
**Serializer / deserializer (Wave-1 build).** A canonical serializer (`orjson`, deterministic key
order) for the envelope `payload`, shared by the in-process path and the future Redpanda path, so the
signed-envelope bytes (ADR-0007) are identical across transports. **ADR-0007 hook:** the outbox row's
canonical bytes are signed via `platform.crypto` (KMS) on publish and verified before handler
dispatch; Wave-1 wires the hook points (sign-on-write, verify-before-deliver) using the existing KMS
facade — quarantine on invalid/missing signature.
**Retry model.** Dispatcher `RetryPolicy(max_attempts)` per registration; on all-handlers-success →
`dispatched`; on any failure → `pending` (retry w/ backoff) until `max_attempts` → `dead_letter`
(logged, metered). **Wave-1 add:** a first-class **dead-letter surface** (query + metric
`outbox_dead_letter_total`) so a poisoned forensic/event row is never silently lost (validation
finding #12).
**Dependencies.** db session factory, `orjson`, KMS (ADR-0007). **Startup order.** Consumers
registered then dispatcher started (worker, post-ADR-0006). **Failure modes.** Handler failure isolated
per-transaction; one bad poll cycle never kills the loop; graceful drain on shutdown. **Testing.**
Unit: outbox same-transaction, inbox dedupe, retry→dead-letter; integration (Testcontainers): two
dispatcher replicas + `SKIP LOCKED` partition rows without double-processing, per-aggregate order
holds. **Future extensibility.** Redpanda producer/consumer replaces the relay; signature travels in
the message header; verification logic unchanged (ADR-0007).

---

## 14. Security foundation (primitives only)

**Purpose.** The cryptographic *primitives* the platform needs — **not** the ADR-0010 session/RBAC
model (later wave). Everything key-related goes through `platform.crypto` (ADR-0009), already
delivered.
**Folder.** `platform/security/*` (Wave-1 build): `hashing.py`, `tokens.py`, `secrets.py`, `tls.py`.
- **Password hashing.** `PasswordHasher` port + `Argon2PasswordHasher` (argon2id, tuned
  time/memory/parallelism per security-architecture) with `verify` + `needs_rehash`. Never store or
  log a plaintext password.
- **Secret loading.** A thin `secrets.py` that reads only from `Settings` (`SecretStr`) — the single
  place that unwraps a secret, and only at point-of-use; never into a log or exception.
- **Token generation.** `TokenGenerator` producing high-entropy opaque tokens (`secrets.token_urlsafe`,
  ≥256-bit) with a short lookup prefix; per ADR-0010 the store keeps only the **hash** (argon2id/keyed
  HMAC via KMS) — Wave 1 delivers the generator + hashing primitive; the session table/flow is later.
- **Secure random.** Standardize on `secrets`/`os.urandom` only; a lint/architecture test forbids
  `random` for security use.
- **TLS assumptions.** TLS terminates at the ingress/reverse proxy (deployment-architecture);
  in-cluster mTLS between services is a post-extraction concern (validation finding #13, later wave).
  App assumes it receives already-terminated HTTPS and trusts `X-Forwarded-*` only from the known
  proxy.
- **Certificate loading.** For providers that need client certs (e.g. PKCS#11/HSM, mTLS to Vault in
  classified), certs/keys are loaded from Vault+ESO-mounted paths via config — never embedded, never
  committed.
**Dependencies.** `argon2-cffi`, `cryptography`, `platform.crypto`. **Startup order.** Hasher built as
a singleton in the composition root. **Failure modes.** Hashing/verify are constant-time; a KMS-backed
token hash failure fails closed. **Testing.** Unit: hash/verify/needs_rehash, token entropy/length,
no-`random` architecture test, secret never appears in `repr`/logs. **Future extensibility.** The
ADR-0010 session/MFA/SSO model and RBAC/ABAC (`case_members`) build on these primitives in the auth
wave; a `mypy`-checked port keeps the hasher swappable (e.g. FIPS module).

---

## 15. Testing foundation

**Purpose.** Make the DoD's quality bar executable and fast, with real dependencies where it matters.
**Folder.** `tests/{unit,integration,contract,performance,architecture,fixtures,factories}` +
`conftest.py`.
**pytest.** `asyncio_mode=auto`, `--strict-markers`, coverage gate (≥90% on `platform`). Markers:
`unit`, `integration`, `contract`, `architecture`, `slow`.
- **Fixtures.** App via `asgi-lifespan`; a Settings-override fixture per profile; DB session/UoW
  fixture with rollback isolation; ephemeral KMS dev keystore (tmp).
- **Fake providers.** In-memory `ObjectStorage`, `Cache`, `RateLimiter`, and a fake `CryptoProvider`
  for unit tests — real adapters covered by integration tests. Fakes live in `tests/fixtures`.
- **Testcontainers.** Postgres, Redis, MinIO (+ Vault dev for the KMS Vault contract test) spun up for
  integration tests; skipped with a clear message when Docker/containers are unavailable (mirrors the
  existing KMS Vault contract-test skip pattern).
- **Contract testing.** Provider-port contract tests run the **same** suite against every adapter
  (fake + real) so a new KMS/object-store/cache adapter must satisfy the identical contract.
- **Integration testing.** End-to-end through real dependencies: migrations round-trip, outbox→
  dispatcher→inbox flow, `/readyz` transitions, object-store multipart round-trip.
- **Architecture tests (Wave-1 build, `tests/architecture`).** Assert, in CI: import-linter contracts
  hold; `platform` imports no `modules`; no cross-schema FK; no raw string-built SQL; no `random` in
  security paths; no secret in logs; every evidentiary table (later) is INSERT/SELECT-only. These are
  the automated enforcement of INV-5 and the governance rules.
**Dependencies.** `pytest`, `pytest-asyncio`, `pytest-cov`, `import-linter`, `factory-boy`,
`asgi-lifespan`, Testcontainers. **Startup order.** n/a. **Failure modes.** A missing container skips
(never false-passes) the integration layer but architecture+unit always run. **Future extensibility.**
Module test suites drop into the same layout; contract suites gain adapters for free.

---

## 16. Docker (development stack)

**Purpose.** One command brings up a faithful local platform (same containers/topology as production,
per system-design §13).
**Folder.** `docker-compose.dev.yml`, `Dockerfile`, `Makefile`.
- **Development stack (compose services).** `server-http`, `server-worker`, `postgres`, `redis`,
  `minio` (+ `createbuckets` init), `vault` (dev mode, for KMS Vault path). Redpanda/Qdrant remain
  **provisioned but commented out** (turned on at extraction/AI waves) — architecture.md Tech Stack.
- **Compose.** Per-service `healthcheck`s so `server-*` wait for `postgres/redis/minio` healthy;
  `depends_on: condition: service_healthy`. Env from `.env` (dev placeholders only).
- **Volumes.** Named volumes for Postgres data, MinIO data, Vault dev data — so local state persists
  across restarts; a `make reset` target wipes them.
- **Networking.** One user-defined bridge network; only the reverse proxy / `server-http` port and
  MinIO/Vault consoles exposed to the host; internal services talk over the compose network.
- **Dockerfile.** Multi-stage (builder installs deps into a venv; slim non-root runtime), from an
  approved base image (deployment §5); `HEALTHCHECK` hits `/healthz`; runs `uvicorn`
  (`server-http`) or `arq` (`server-worker`) by entrypoint arg. Read-only rootfs where feasible.
**Dependencies.** Docker. **Startup order.** compose `depends_on` healthchecks encode it. **Failure
modes.** A dependency container unhealthy → `server-*` waits, `/readyz` 503. **Testing.** CI can boot
the compose stack for a smoke test. **Future extensibility.** The same images deploy to Kubernetes
(deployment-architecture); air-gapped variant pulls every image from an internal registry, no egress.

---

## 17. CI/CD

**Purpose.** Make the governance §11 pipeline the enforcement mechanism — a change that would fail CI
is not done.
**Folder.** `.github/workflows/ci.yml` (Wave-1 build), `.github/workflows/release.yml`.
**Pipeline stages (hard gates unless noted), on every PR:**
1. **Format** — `ruff format --check`. 2. **Lint** — `ruff check`. 3. **Type** — `mypy --strict`.
4. **Architecture validation** — `import-linter` (both contracts) + `tests/architecture`.
5. **Unit + coverage** — `pytest -m "unit or architecture"`, coverage ≥ 90% on `platform`.
6. **Integration** — `pytest -m integration` with service containers (Postgres/Redis/MinIO/Vault).
7. **Security scan** — SAST (`ruff`/`bandit`-class) + secret scan (advisory→gating as it matures).
8. **Dependency scan + SBOM** — vulnerability + license policy; **CycloneDX SBOM** generated and
   stored as an artifact.
9. **Container build + scan + sign** — build image, scan CVEs, **cosign sign**; only approved base.
10. **Migration check** — `alembic upgrade head` then `downgrade base` on a clean container DB.
11. **Deploy gates** (release workflow) — GitOps only; images signed; ArgoCD PreSync migrations in
    module-DAG order; validated rollback exercised in staging (governance deployment rules).
**Failure modes.** Any gate red blocks merge; a bypass needs a labeled, time-boxed, board-approved
exception in the risk register. **Testing.** The workflow is itself validated by running on the Wave-1
PRs. **Future extensibility.** DAST, performance-regression, and per-module coverage gates slot in as
those waves land.

---

## 18. Code quality

**Purpose.** Uniform style, typing, and review discipline enforced by tooling + process.
- **ruff** — lint (`E,F,I,UP,B,C4,SIM,TID,RUF`) **and format**. `ruff format` is the formatter;
  it is black-compatible style, so the frozen stack uses **ruff format in place of black** (no
  separate `black` dependency — this reconciles the ask with ADR-0002's tooling, not a new decision).
- **mypy** — `strict`, `pydantic.mypy` plugin, `disallow_untyped_defs`, `warn_unused_ignores`. No
  `type: ignore` without a justification comment.
- **pre-commit (Wave-1 add `.pre-commit-config.yaml`)** — ruff (lint+format), mypy (fast subset),
  end-of-file/trailing-whitespace, detect-secrets, import-linter. Runs the same checks CI enforces so
  failures surface locally.
- **Commit conventions** — Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`), imperative subject; body explains *why*; footer references the ADR/task. Commits are
  co-authored per repo policy.
- **Branch strategy** — trunk-based off `main`; short-lived `feat/<slug>` branches; no direct pushes
  to `main`; branch protection requires green CI + review. Never commit/push unless asked.
- **PR checklist (Wave-1 build `.github/pull_request_template.md`)** — references the frozen doc/ADR;
  gates pass; tests added; `downgrade()` present for any migration; no secret/TODO/stub; docs updated
  in the same PR; DoD (§1 / governance §13) satisfied.
- **Definition of Done** — governance §13 in full, plus this doc's §1 for foundation work.

---

## 19. Global startup & shutdown order

**HTTP entrypoint (`create_app` + lifespan):**
1. `settings.validate_for_profile()` → 2. `configure_logging` → 3. DB engine → 4. Redis pool →
5. object-storage client (+ scratch bucket) → 6. KMS `create_kms().start()` + health (**fail closed
in prod**) → 7. dispatcher: `register_consumers` then start (post-ADR-0006: in the worker) →
8. arq pool (best-effort) → 9. middleware, exception handlers, health router, module routers,
`/metrics`.
**Shutdown (reverse):** request dispatcher drain → await drain → close arq pool → KMS `aclose()` →
dispose DB engine → final log. **Worker entrypoint:** same singletons via `on_startup`; **hosts the
relocated dispatcher** (ADR-0006) and the scheduler hook for periodic platform jobs (idempotency-key
cleanup, outbox dead-letter sweep — the anchoring/verification schedulers are later waves).

---

## 20. Wave 1 task list (ordered, with acceptance)

Effort: **S** ≤2d · **M** ≈3–5d · **L** ≈1–2wk. All owned by the Platform & Infrastructure squad.
Ordered by dependency.

| # | Task | Effort | Depends | Acceptance (Definition of Done) |
|---|---|---|---|---|
| W1-01 | Profiles + `validate_for_profile()` + startup validation; reconcile legacy JWT config fields against ADR-0010 (mark reserved, not active) | M | — | 5 profiles validate; prod/classified reject placeholder secret+`dev` KMS; startup aborts on invalid config |
| ~~W1-02~~ | Error taxonomy + envelope handler — **already existed** (`shared/exceptions.py` guide Part 11 + `exception_handlers.py`). No build task; a duplicate `platform/errors.py` attempt was reverted. | — | — | N/A — component pre-existing and gate-green |
| W1-03 | PII-masking log processor + W3C `traceparent` propagation | S | W1-01 | secrets masked in captured logs; `trace_id` populated from inbound `traceparent` |
| W1-04 | `platform/observability/metrics.py` (RED/USE/platform helpers + naming guard) | S | W1-01 | metrics render with convention; cardinality guard test green |
| W1-05 | DB role separation (ADR-0004) migration + runtime engine uses `sentinel_app`/`append`; repository base class | M | W1-01 | roles created; runtime role has no `UPDATE/DELETE` on evidentiary tables; repo base persists-only |
| W1-06 | Redis foundation: `Cache`, `RateLimiter`, pool, fail-closed policy | M | W1-01 | cache hit/miss/expiry, rate-limit window, fail-closed tests green |
| W1-07 | Object-storage port + MinIO adapter + factory + scratch-bucket bootstrap + `/readyz` check | M | W1-01 | stream + multipart + presign round-trip against Testcontainers MinIO |
| W1-08 | Security primitives: argon2id hasher, secure token generator, secret loader, TLS/cert assumptions | M | W1-01 | hash/verify/needs_rehash, token entropy, no-`random` architecture test green |
| W1-09 | Event serializer (orjson canonical) + ADR-0007 sign-on-write / verify-before-deliver hooks | M | W1-05, KMS | invalid/missing signature → quarantine; canonical bytes stable across transports |
| W1-10 | Dispatcher → ADR-0006: relocate to worker, `SELECT … FOR UPDATE SKIP LOCKED`, per-aggregate ordering, backoff, dead-letter surface+metric | L | W1-09 | two replicas partition rows w/o double-process; per-aggregate order holds; dead-letter queryable+metered |
| W1-11 | Health: object-store check, startup probe, cached readiness | S | W1-07 | `/startupz` holds until ready; `/readyz` lists every failing dep; TTL cache bounds probe load |
| W1-12 | DI composition-root cleanup: factories for every port, lifetimes, no service-locator | S | W1-06..08 | all ports built in composition root; `dependency_overrides` swap fakes in tests |
| W1-13 | Testing foundation: Testcontainers, fake providers, contract + architecture test suites | L | W1-05..10 | contract suite runs against fake+real adapters; architecture tests enforce INV-5 + governance rules |
| W1-14 | Docker dev stack: add MinIO/Vault, healthchecks, volumes, network, multi-stage non-root Dockerfile | M | W1-07 | `make up` boots healthy stack; smoke test green |
| W1-15 | CI/CD pipeline + pre-commit + PR template + branch protection | M | all | full pipeline green on a Wave-1 PR; SBOM + signed image produced |
| W1-16 | Migration round-trip + downgrade tests + naming lint in CI | S | W1-05 | `upgrade head`→`downgrade base` green in CI; every migration has tested `downgrade()` |

**Critical path:** W1-01 → W1-05 → W1-09 → W1-10 → W1-13 → W1-15. W1-06/07/08 parallelize after
W1-01. Wave 1 exits when §1's bar is met and every task above is accepted.

---

*Keep this manual synchronized with the frozen corpus: if an implementation blocker forces a design
change, raise an ADR (governance §2) and update the affected frozen doc in the same change — never
silently diverge the build from the design.*
