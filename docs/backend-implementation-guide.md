# SentinelAI — Backend Implementation Guide

**Status:** Authoritative Implementation Guide
**Last updated:** 2026-07-19
**Related documents:** [PRD](prd.md) · [Architecture](architecture.md) · [System Design](system-design.md) · [Database Design](database-design.md) · [API Design](api-design.md) · [Event-Driven Architecture](event-driven-architecture.md) · [Canonical Evidence Model](canonical-evidence-model.md) · [Security Architecture](security-architecture.md)

**Architecture explains WHAT. This guide explains HOW.** Every document listed above describes *what* `apps/server` must do, without assuming a language or framework. This document is not another architecture document — it is the concrete, language-specific implementation standard every line of backend code must follow. **No implementation may violate this guide.** Where this guide is silent, the architecture documents govern; where they overlap, this guide's specifics are how those architectural requirements get satisfied in code, and it does not restate their reasoning — it references it.

**Stack decision, made here, definitively:** `apps/server` is implemented in **Python** with **FastAPI** (HTTP framework), **SQLAlchemy 2.0** (async ORM), **Alembic** (migrations), and **Pydantic v2** (validation/serialization). This resolves `architecture.md`'s and `database-design.md` §11's open "`apps/server` language/framework" ADR item and `database-design.md` §11's "migration tooling not yet chosen" item. Per `CLAUDE.md`'s standing convention, this should still be formally recorded as an ADR — this document is the technical content that ADR would point to. Supporting library choices made throughout this guide (`structlog`, `arq`, `argon2-cffi`, `import-linter`, `aioboto3`) are committed decisions for the same reason: this guide's purpose is to remove ambiguity, not preserve it.

**This document does not duplicate the architecture documents.** Every table/endpoint/event/security rule referenced below is defined authoritatively elsewhere and cited by section number; this guide shows *how* to build what they specify.

### Contents

| Part | Topic | Part | Topic |
|---|---|---|---|
| 1 | Coding Standards | 11 | Error Handling |
| 2 | FastAPI Standards | 12 | Background Jobs |
| 3 | SQLAlchemy Standards | 13 | Testing Standards |
| 4 | Alembic | 14 | Performance |
| 5 | Services | 15 | Security Rules |
| 6 | Events | 16 | Code Review Checklist |
| 7 | API Implementation | 17 | AI Coding Rules |
| 8 | Authentication | 18 | Anti-Patterns |
| 9 | File Handling | 19 | Examples |
| 10 | Logging | 20 | Cross References |

---

# Part 1 — Coding Standards

## Project Layout

```
apps/server/
├── pyproject.toml
├── src/sentinelai/
│   ├── entrypoints/
│   │   ├── http/               # entrypoints/http (system-design.md §2)
│   │   │   ├── main.py           # FastAPI app, lifespan, router registration
│   │   │   ├── middleware.py
│   │   │   └── exception_handlers.py
│   │   └── worker/              # entrypoints/worker (system-design.md §2)
│   │       └── main.py           # arq WorkerSettings
│   ├── platform/                # apps/server/platform (system-design.md §2)
│   │   ├── db/{base.py,session.py,uow.py}
│   │   ├── auth/{dependencies.py,models.py}
│   │   ├── events/{envelope.py,outbox.py,inbox.py,dispatcher.py}
│   │   ├── logging.py
│   │   └── config.py
│   ├── shared/                  # cross-module utilities — no business logic
│   │   ├── pagination.py
│   │   ├── envelope.py
│   │   └── exceptions.py
│   └── modules/
│       ├── ingestion/  osint/  threat_intel/  forensics/  social_media/
│       ├── case_management/  investigation/  notification/
│       │   ├── __init__.py
│       │   ├── public.py         # the ONLY symbols another module may import
│       │   ├── models.py         # SQLAlchemy models, schema=<module>
│       │   ├── schemas.py        # Pydantic request/response models
│       │   ├── repository.py
│       │   ├── service.py
│       │   ├── events.py         # publish + consume handlers
│       │   ├── router.py
│       │   ├── exceptions.py
│       │   └── migrations/{env.py,versions/}
└── tests/{unit/,integration/,contract/,conftest.py}
```

Every top-level `modules/*` folder maps 1:1 to a `database-design.md` §2 Postgres schema and an `event-driven-architecture.md` §25 catalog entry — a module folder that doesn't correspond to both is a structural error.

## Module Boundaries & Public Interface

Each `modules/<name>/public.py` re-exports the **only** symbols other modules or entrypoints may import from that module — its service class(es) and its Pydantic schemas, never its `models.py` (ORM), `repository.py`, or internals:

```python
# sentinelai/modules/case_management/public.py
from sentinelai.modules.case_management.service import CaseService
from sentinelai.modules.case_management.schemas import CaseRead, CaseCreate

__all__ = ["CaseService", "CaseRead", "CaseCreate"]
```

```python
# FORBIDDEN, anywhere outside modules/case_management/:
from sentinelai.modules.case_management.models import Case          # deep import
from sentinelai.modules.case_management.repository import CaseRepository  # deep import

# REQUIRED:
from sentinelai.modules.case_management.public import CaseService
```

## Naming Conventions

`snake_case` for modules, functions, variables, files; `PascalCase` for classes; `SCREAMING_SNAKE_CASE` for module-level constants; test files `test_*.py` mirroring the file under test (`service.py` → `test_service.py`). Router files are always `router.py`, never `routes.py`/`views.py`/`endpoints.py` — one name, everywhere, so navigation is predictable across all nine module folders.

## Import Rules & Dependency Direction

Absolute imports only — no `from ..other_module import x`. The **import graph must match `database-design.md` §5's dependency DAG exactly**: `platform` → `ingestion` → `{osint, threat_intel, forensics, social_media, case_management}` → `investigation` → `notification`. A module may import `platform`, `shared`, and any module *earlier* in this order via its `public.py` — never a module later in the order, and never (with the single documented exception of `investigation`, per `event-driven-architecture.md` §5) a sibling at the same tier.

## Circular Dependency Prevention

Enforced mechanically in CI with `import-linter`, not by convention alone:

```ini
# .importlinter
[importlinter]
root_package = sentinelai

[importlinter:contract:module-boundaries]
name = Modules only import public interfaces
type = forbidden
source_modules =
    sentinelai.modules.case_management
forbidden_modules =
    sentinelai.modules.investigation
    sentinelai.modules.notification

[importlinter:contract:no-deep-imports]
name = No deep imports into another module's internals
type = layers
layers =
    public
containers =
    sentinelai.modules.case_management
```

A PR that violates the dependency DAG fails this check before human review ever sees it — extending the existing `pr-validation.yml` pattern (`.github/workflows/`).

---

# Part 2 — FastAPI Standards

## Routers

One `APIRouter` per module, registered in `entrypoints/http/main.py` with the exact prefix `api-design.md` §4 documents:

```python
# sentinelai/modules/case_management/router.py
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/cases", tags=["cases"])
```

```python
# sentinelai/entrypoints/http/main.py
from sentinelai.modules.case_management.router import router as cases_router
from sentinelai.modules.ingestion.router import router as evidence_router

app.include_router(cases_router)
app.include_router(evidence_router)
```

## Dependency Injection

```python
# sentinelai/platform/db/session.py
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session

# sentinelai/platform/db/uow.py
async def get_unit_of_work(session: AsyncSession = Depends(get_session)) -> UnitOfWork:
    return UnitOfWork(session)
```

## Lifespan

`@app.on_event` is deprecated — use the `lifespan` context manager, and it is where the Phase 1 event dispatcher (`event-driven-architecture.md` §2) starts and stops, respecting §2.2's graceful-shutdown requirement:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_async_engine(settings.database_url, pool_size=20, max_overflow=10)
    app.state.engine = engine
    dispatcher = EventDispatcher(engine)
    dispatcher_task = asyncio.create_task(dispatcher.run_forever())
    yield
    dispatcher.request_shutdown()
    await dispatcher_task  # drains in-flight handler invocations before exiting
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
```

## Middleware

Correlation/request ID propagation (`api-design.md` §2.8):

```python
@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    request.state.request_id = str(uuid4())
    request.state.correlation_id = request.headers.get("X-Correlation-Id", str(uuid4()))
    with structlog.contextvars.bound_contextvars(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
    ):
        response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    response.headers["X-Correlation-Id"] = request.state.correlation_id
    return response
```

## Exception Handlers

```python
@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {
            "code": exc.code, "message": str(exc), "details": exc.details,
            "request_id": request.state.request_id,
            "correlation_id": request.state.correlation_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }},
    )

@app.exception_handler(RequestValidationError)
async def shape_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Overrides FastAPI's default 422-for-everything so it matches api-design.md §2.4's
    # 400 (malformed shape) vs 422 (domain rule) split — Part 7 explains this in full.
    return JSONResponse(status_code=400, content={"error": {
        "code": "VALIDATION_FAILED", "message": "Malformed request",
        "details": exc.errors(), "request_id": request.state.request_id,
        "correlation_id": request.state.correlation_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }})
```

## Request Validation, Response Models, Pagination, Filtering, Sorting

Request bodies are Pydantic v2 models; every route declares `response_model=Envelope[SomeReadModel]`, **never** an ORM model. Cursor pagination and filter/sort query parameters are implemented once in `shared/pagination.py` and reused by every list endpoint — shown in full in Part 19.

---

# Part 3 — SQLAlchemy Standards

## ORM Conventions

One shared `Base`, every model sets its module's Postgres schema explicitly (`database-design.md` §1):

```python
# sentinelai/platform/db/base.py
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

```python
# sentinelai/modules/ingestion/models.py
from sqlalchemy import String, DateTime, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sentinelai.platform.db.base import Base

class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_category_artifact_type", "category", "artifact_type"),
        {"schema": "ingestion"},
    )

    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_validation")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

No cross-schema `ForeignKey` is ever declared, matching `database-design.md` §5 exactly — an inter-schema reference (e.g. `case_evidence_links.evidence_id`) is a plain `PGUUID` column with **no** `ForeignKey(...)`.

## Repositories

```python
# sentinelai/modules/ingestion/repository.py
from sqlalchemy import select

class EvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, evidence_id: UUID) -> Evidence | None:
        result = await self._session.execute(select(Evidence).where(Evidence.evidence_id == evidence_id))
        return result.scalar_one_or_none()

    async def add(self, evidence: Evidence) -> None:
        self._session.add(evidence)
```

A repository knows only its own module's models — it structurally cannot query another module's tables, since it never imports them.

## Unit of Work & Transactions

```python
# sentinelai/platform/db/uow.py
class UnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.evidence = EvidenceRepository(session)
        self.outbox = OutboxWriter(session, schema="ingestion")

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
```

One transaction per request/use-case, committed once at the end of a successful service call, rolled back entirely on any exception — never a partial commit within one logical operation. The outbox write (Part 6) happens through the *same* `session`, inside the *same* transaction, which is what gives `event-driven-architecture.md` §16's atomicity guarantee.

## Eager vs. Lazy Loading

The async engine already **forbids implicit lazy loading** (accessing an unloaded relationship raises rather than issuing a surprise query) — this is a feature, not friction: every relationship access is an explicit, reviewed decision.

```python
result = await session.execute(
    select(Case).options(selectinload(Case.evidence_links)).where(Case.case_id == case_id)
)
```

## Query Optimization, Indexes, Bulk Operations

Use `.exists()` for existence checks instead of a full fetch; batch lookups with `.where(Model.id.in_(ids))` instead of N queries. Indexes declared in the model match `database-design.md` §6 exactly, including BRIN for time-ordered append-only tables:

```python
__table_args__ = (
    Index("ix_evidence_ingested_at_brin", "ingested_at", postgresql_using="brin"),
    {"schema": "ingestion"},
)
```

Batch ingestion (`api-design.md` §2.10) uses Core bulk `insert()`, never a loop of ORM object creation:

```python
from sqlalchemy import insert
await session.execute(insert(Evidence.__table__), [row.model_dump() for row in batch_rows])
```

---

# Part 4 — Alembic

## Migration Rules & Per-Module Ownership

Every module owns its **own Alembic environment**, its own `versions/` directory, and — critically — its own version-tracking table, placed *inside that module's own schema* (`database-design.md` §11's per-module migration ownership, made concrete):

```python
# sentinelai/modules/ingestion/migrations/env.py
from alembic import context
from sentinelai.platform.db.base import Base
import sentinelai.modules.ingestion.models  # noqa: F401 — registers models on Base.metadata

def run_migrations_online() -> None:
    with sync_engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table="alembic_version",
            version_table_schema="ingestion",
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
```

A migration runner script applies every module's migrations in `database-design.md` §5's DAG order (`platform` → `ingestion` → …) against the one Phase 1 database — see Part 20 for the cross-reference.

## Migration Naming

`<YYYYMMDDHHMM>_<module>_<slug>.py`, e.g. `202607190001_ingestion_create_evidence.py` — sortable, module-scoped, self-describing without opening the file.

## Reversible Migrations

Every migration implements both `upgrade()` and `downgrade()` — a `downgrade()` that is only `pass` is a review-blocking defect, not an accepted shortcut:

```python
def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("attributes", postgresql.JSONB, nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        schema="ingestion",
    )

def downgrade() -> None:
    op.drop_table("evidence", schema="ingestion")
```

## Data Migrations

Kept as separate revisions from schema migrations (add-column vs. backfill-column), batched (never one unbounded `UPDATE` against `evidence`), idempotent (safe to re-run), and **legal-hold-aware** — a data migration touching `evidence` must filter out `legal_hold = true` rows or explicitly justify why it's exempt, per `database-design.md` §7/§12.

## Schema Versioning

An Alembic revision that changes the shape backing a CEM MAJOR version bump (`canonical-evidence-model.md` §12) must land in the same change as the corresponding `event-driven-architecture.md` §23 event-version bump and `api-design.md` §14 API-version bump — three views of one change, recorded together.

---

# Part 5 — Services

## Business Logic Placement

Routers **parse and delegate**, never contain business rules. Repositories **persist**, never contain business rules. **All** business logic — CEM §13 validation, state-machine transitions, cross-module orchestration — lives in `service.py`.

```python
# sentinelai/modules/investigation/service.py
class RelationshipReviewService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def review(self, relationship_id: UUID, disposition: str, note: str | None,
                      actor: CurrentUser, correlation_id: str) -> Relationship:
        relationship = await self._uow.relationships.get_by_id(relationship_id)
        if relationship is None:
            raise NotFoundError()
        if relationship.status != "proposed":
            raise ConflictError(f"relationship is already {relationship.status}")

        relationship.status = disposition
        await self._uow.relationship_revisions.add(RelationshipRevision(
            relationship_id=relationship_id, previous_status="proposed", new_status=disposition,
        ))
        await self._uow.outbox.publish(
            event_type="investigation.finding_reviewed", aggregate_type="relationship",
            aggregate_id=relationship_id,
            payload={"case_id": str(relationship.case_id), "disposition": disposition},
            correlation_id=correlation_id, actor_type="user", actor_ref=actor.user_id,
        )
        return relationship
```

## Orchestration & Cross-Module Calls

Cross-module orchestration happens **service-to-service**, always through the other module's `public.py`, never its repository:

```python
from sentinelai.modules.ingestion.public import EvidenceService  # not EvidenceRepository

class CaseEvidenceLinkService:
    def __init__(self, uow: UnitOfWork, evidence_service: EvidenceService) -> None:
        self._uow = uow
        self._evidence_service = evidence_service

    async def link(self, case_id: UUID, evidence_id: UUID, actor: CurrentUser) -> None:
        if not await self._evidence_service.exists(evidence_id):
            raise NotFoundError()
        ...
```

## Validation Responsibilities

Three layers, never merged: **Pydantic** (shape/type, Part 2) → **service** (CEM §13 business rules, state transitions, attribute-schema-registry lookups — things Pydantic cannot know at class-definition time) → **database** (schema constraints, the module's own referential integrity). A rule belongs in exactly one layer; duplicating it across layers is drift waiting to happen.

---

# Part 6 — Events

Implements `event-driven-architecture.md` in full — this Part shows *how*, that document defines *what/why/policy*.

## Outbox Writing

```python
# sentinelai/platform/events/outbox.py
class OutboxWriter:
    def __init__(self, session: AsyncSession, schema: str) -> None:
        self._session, self._schema = session, schema

    async def publish(self, *, event_type: str, aggregate_type: str, aggregate_id: UUID,
                       payload: dict, correlation_id: str, actor_type: str,
                       actor_ref: UUID | None = None, causation_id: str | None = None,
                       event_version: str = "1.0.0") -> None:
        table = get_outbox_table(self._schema)
        await self._session.execute(insert(table).values(
            event_id=uuid4(), event_type=event_type, event_version=event_version,
            aggregate_type=aggregate_type, aggregate_id=aggregate_id, payload=payload,
            correlation_id=correlation_id, causation_id=causation_id,
            actor_type=actor_type, actor_ref=actor_ref,
            occurred_at=datetime.now(UTC), dispatch_status="pending", attempt_count=0,
        ))
```

Called only from within a service method, on the same `session` the UoW already opened — never a second connection, never after the surrounding `commit()`.

## Inbox Deduplication

```python
# sentinelai/platform/events/inbox.py
class InboxGuard:
    def __init__(self, session: AsyncSession, schema: str) -> None:
        self._session, self._schema = session, schema

    async def try_claim(self, event_id: UUID, handler_name: str) -> bool:
        table = get_inbox_table(self._schema)
        try:
            await self._session.execute(insert(table).values(
                event_id=event_id, handler_name=handler_name,
                received_at=datetime.now(UTC), processing_status="processing",
            ))
            return True
        except IntegrityError:
            await self._session.rollback()
            return False  # already claimed by this handler — redelivery, skip
```

## Consumer Implementation

```python
# sentinelai/modules/threat_intel/events.py
async def on_evidence_ingested(event: EventEnvelope, uow: UnitOfWork) -> None:
    guard = InboxGuard(uow.session, schema="threat_intel")
    if not await guard.try_claim(event.event_id, handler_name="scan_for_ioc_matches"):
        return
    matches = await uow.iocs.find_matching(event.payload["category"], event.aggregate_id)
    for ioc in matches:
        await uow.outbox.publish(
            event_type="threat_intel.ioc_matched", aggregate_type="ioc", aggregate_id=ioc.ioc_id,
            payload={"matched_evidence_id": str(event.aggregate_id), "confidence": ioc.confidence},
            correlation_id=event.correlation_id, causation_id=str(event.event_id), actor_type="system",
        )
```

Registered against the Phase 1 in-process `EventDispatcher` (`event-driven-architecture.md` §2) by `event_type`; retry/DLQ policy per §14–15 is the dispatcher's responsibility, not the handler's.

---

# Part 7 — API Implementation

Reference: `api-design.md` in full. This Part traces one endpoint — `PATCH /api/v1/relationships/{relationship_id}/status` (`api-design.md` §6) — end to end.

## Request Flow

```
HTTP request
  → correlation middleware (Part 2) binds request_id/correlation_id
  → FastAPI routes to router.py:update_relationship_status
  → Pydantic validates the body shape (RelationshipStatusUpdate)
      — malformed shape → shape_validation_handler → 400 (Part 2's override)
  → DI resolves: get_current_user → require_role("investigator") → require_case_access()
  → DI resolves: get_unit_of_work, get_relationship_review_service
  → router calls service.review(...)
  → service raises a DomainError on a business rule failure → domain_error_handler → 422/409/404
  → service returns the updated Relationship ORM object
  → router maps it to RelationshipRead (Pydantic), wraps in Envelope
```

## Router

```python
# sentinelai/modules/investigation/router.py
@router.patch("/{relationship_id}/status", response_model=Envelope[RelationshipRead])
async def update_relationship_status(
    relationship_id: UUID,
    payload: RelationshipStatusUpdate,
    request: Request,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("investigator")),
    _: CurrentUser = Depends(require_case_access()),
    service: RelationshipReviewService = Depends(get_relationship_review_service),
) -> Envelope[RelationshipRead]:
    relationship = await service.review(
        relationship_id, payload.status, payload.note, current_user, request.state.correlation_id,
    )
    return Envelope(
        data=RelationshipRead.model_validate(relationship),
        meta=Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id),
    )
```

## Response Flow & "Problem Details"

`api-design.md` §2.4's error envelope is a **deliberate, already-settled custom shape** — not literal RFC 7807. Part 2's exception handlers are what implement it; there is no separate "Problem Details" library or middleware layered on top — the handlers *are* the mapping, shown in full there. A successful response always serializes through `Envelope[T]`; an error response always serializes through the shape in Part 2's `domain_error_handler` — there is no third response shape anywhere in the API.

---

# Part 8 — Authentication

Reference: `security-architecture.md` §5–9 in full.

## Current User & RBAC

```python
# sentinelai/platform/auth/dependencies.py
async def get_current_user(
    authorization: str = Header(...),
    session_repo: SessionRepository = Depends(get_session_repository),
) -> CurrentUser:
    if not authorization.startswith("Bearer "):
        raise UnauthenticatedError()
    session = await session_repo.get_active_by_token(authorization.removeprefix("Bearer "))
    if session is None or session.expires_at < datetime.now(UTC):
        raise UnauthenticatedError()
    return CurrentUser(user_id=session.user_id, roles=session.roles)


def require_role(*allowed: str):
    async def _dep(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not set(current_user.roles) & set(allowed):
            raise ForbiddenError()
        return current_user
    return _dep
```

## ABAC / Case-Scope Permissions

```python
def require_case_access(param: str = "case_id"):
    async def _dep(
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
        case_repo: CaseRepository = Depends(get_case_repository),
    ) -> CurrentUser:
        case_id = UUID(request.path_params[param])
        if not await case_repo.user_has_access(case_id, current_user.user_id):
            raise ForbiddenError()
        return current_user
    return _dep
```

This is `security-architecture.md` §6's authorization flow diagram, implemented — RBAC (`require_role`) gates the action class, ABAC (`require_case_access`) gates the specific resource, both audited (Part 10) regardless of outcome.

## Tenant Context

Phase 1 is single-tenant (`security-architecture.md` §40 is future work) — no `tenant_id` filtering exists yet. A `tenant_id: ContextVar[UUID | None]` is reserved in `platform/config.py`, currently always `None`, so the extension point exists without speculative implementation ahead of the Phase 4 ADR that section requires.

---

# Part 9 — File Handling

Implements `api-design.md` §2.11 and `security-architecture.md` §24–26.

## Storage Abstraction

```python
# sentinelai/platform/storage.py
class ObjectStorage(Protocol):
    async def presigned_upload_url(self, bucket: str, key: str, expires_in: int) -> str: ...
    async def presigned_download_url(self, bucket: str, key: str, expires_in: int) -> str: ...
    async def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None: ...

class S3CompatibleStorage(ObjectStorage):
    def __init__(self, client: aioboto3.Session) -> None:
        self._client = client

    async def presigned_upload_url(self, bucket: str, key: str, expires_in: int = 900) -> str:
        async with self._client.client("s3") as s3:
            return await s3.generate_presigned_url(
                "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in,
            )
```

## Upload Flow & Quarantine

```python
async def reserve_upload(self, category: str, artifact_type: str, actor: CurrentUser) -> UploadReservation:
    evidence_id = uuid4()
    key = f"{evidence_id}"
    url = await self._storage.presigned_upload_url(bucket="quarantine", key=key, expires_in=900)
    return UploadReservation(evidence_id=evidence_id, upload_url=url)
```

Promotion from `quarantine` to `evidence` bucket happens only after a background job (Part 12) confirms a clean scan — never inline in the request path, matching `security-architecture.md` §24's "never transiently reachable unscanned" rule.

## Hash Verification (Streaming)

```python
async def compute_sha256(stream: AsyncIterator[bytes]) -> str:
    hasher = hashlib.sha256()
    async for chunk in stream:
        hasher.update(chunk)  # never load the whole file into memory
    return hasher.hexdigest()
```

## Virus Scanning

A background job (Part 12) invokes the scan engine after upload confirmation; `security-architecture.md` §25's category-aware policy (forensic categories flag-not-block, others block) is enforced in `EvidenceScanService`, not in the job runner itself, keeping that domain rule in the service layer per Part 5.

---

# Part 10 — Logging

## Structured Logging

```python
# sentinelai/platform/logging.py
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()
```

`correlation_id`/`request_id`/`trace_id` are bound once, in Part 2's middleware, and appear on every subsequent log line for that request automatically — no call site manually threads them.

## Audit Logging

```python
# sentinelai/platform/auth/audit.py — the single, unbypassable write path (security-architecture.md §22)
async def record_audit_event(session: AsyncSession, *, actor_user_id: UUID | None, action: str,
                              module: str, target_type: str | None, target_id: UUID | None,
                              details: dict | None = None) -> None:
    prev_hash = await _get_last_entry_hash(session)
    entry_hash = _compute_hash(prev_hash, action, target_id, details)
    await session.execute(insert(audit_log_table).values(
        audit_id=uuid4(), occurred_at=datetime.now(UTC), actor_user_id=actor_user_id,
        action=action, module=module, target_type=target_type, target_id=target_id,
        details=details, prev_entry_hash=prev_hash, entry_hash=entry_hash,
    ))
```

## Metrics & Tracing

```python
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app, endpoint="/metrics")  # api-design.md §12
```

OpenTelemetry instruments FastAPI and SQLAlchemy at startup, using the same `trace_id` format `event-driven-architecture.md` §11 already specifies (W3C Trace Context) — one trace-ID format across HTTP, DB spans, and events.

---

# Part 11 — Error Handling

## Domain Exception Hierarchy

```python
# sentinelai/shared/exceptions.py
class DomainError(Exception):
    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    details: list[dict] = []

class ValidationFailedError(DomainError):
    code, http_status = "VALIDATION_FAILED", 422
    def __init__(self, details: list[dict]) -> None:
        self.details = details
        super().__init__("One or more fields failed validation")

class EvidenceImmutableError(DomainError):
    code, http_status = "EVIDENCE_IMMUTABLE", 409

class LegalHoldViolationError(DomainError):
    code, http_status = "LEGAL_HOLD_VIOLATION", 409

class NotFoundError(DomainError):
    code, http_status = "NOT_FOUND", 404

class ForbiddenError(DomainError):
    code, http_status = "FORBIDDEN", 403

class UnauthenticatedError(DomainError):
    code, http_status = "UNAUTHENTICATED", 401

class ConflictError(DomainError):
    code, http_status = "CONFLICT", 409
```

Every class here maps 1:1 to an `api-design.md` §2.4 error code — adding a new domain exception without a corresponding documented code (or vice versa) is a contract violation, not a local implementation choice.

## Infrastructure Exceptions

Database/object-storage/network errors are caught **at the boundary** (repository or storage-client layer) and translated to a `DomainError` subclass or a generic `INTERNAL_ERROR` — raw `asyncpg.PostgresError` or `botocore` exceptions never reach a router or an HTTP response.

## Retry Rules

Server-side outbound calls (connectors, event handlers) retry per `event-driven-architecture.md` §14's named policies. For API consumers: `5xx`, `429`, and network failures are retryable (with backoff, respecting `Retry-After`); any other `4xx` is not — a `422` will not become valid by retrying, only by fixing the request.

---

# Part 12 — Background Jobs

`arq` (async, Redis-backed — matches the Redis already provisioned, `system-design.md` §9).

## Job Lifecycle

The job's **database row is its state** (`correlation_runs`, `case_reports` — `database-design.md` §3.5, §3.4); the queue is only the execution mechanism, matching `api-design.md` §2.12's async pattern exactly:

```python
# sentinelai/modules/investigation/jobs.py
async def run_correlation(ctx: dict, run_id: UUID) -> None:
    async with AsyncSession(ctx["engine"]) as session:
        uow = UnitOfWork(session)
        run = await uow.correlation_runs.get_by_id(run_id)
        run.status = "running"
        await uow.commit()
        try:
            findings = await CorrelationService(uow).correlate(run.case_id)
            run.status, run.findings_generated_count = "completed", len(findings)
        except Exception:
            run.status = "failed"
            raise
        finally:
            run.completed_at = datetime.now(UTC)
            await uow.commit()
```

## Retries & Progress

```python
class WorkerSettings:
    functions = [run_correlation]
    max_tries = 5              # mirrors event-driven-architecture.md §14's "Standard" policy
    job_timeout = 600
```

A long-running job updates its row's progress fields incrementally, not only at start/end, so `GET /correlation-runs/{id}` (`api-design.md` §6) reflects real interim state rather than a stale "running" with no detail.

## Cancellation

Cooperative, never a forced `task.cancel()` mid-transaction: the job periodically checks a `cancellation_requested` flag on its own row and exits cleanly at the next safe checkpoint, leaving the database in a consistent state.

---

# Part 13 — Testing Standards

## Layers & Fixtures

```python
# tests/conftest.py
@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as s:
        yield s
        await s.rollback()  # every test rolls back — no cross-test data leakage

@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c

@pytest.fixture
def make_relationship(session):
    async def _make(**overrides) -> Relationship:
        defaults = dict(relationship_id=uuid4(), type="located_at", status="proposed", confidence=0.7)
        rel = Relationship(**(defaults | overrides))
        session.add(rel)
        await session.flush()
        return rel
    return _make
```

## Unit / Integration / Contract / Repository / API Tests

- **Unit** — pure logic (a validator function), no fixtures beyond plain objects.
- **Integration** — service + repository against a real test Postgres instance.
- **Contract** — response bodies validated against the Pydantic response models (`RelationshipRead.model_validate_json(...)`), which are themselves the documented shape in `api-design.md`.
- **Repository** — query correctness and, for performance-critical queries, `EXPLAIN`-based index-usage assertions.
- **API** — `httpx.AsyncClient` against the full app with `app.dependency_overrides[get_current_user] = ...` for auth.

```python
@pytest.mark.asyncio
async def test_confirm_relationship_requires_case_access(client: AsyncClient, other_case_relationship):
    response = await client.patch(
        f"/api/v1/relationships/{other_case_relationship.relationship_id}/status",
        json={"status": "confirmed"},
        headers={"Authorization": "Bearer test-token", "If-Match": '"rev-1"'},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
```

---

# Part 14 — Performance

- **Caching:** Redis, cache-aside, for read-heavy/rarely-changing data (`attribute_schema_registry`, roles) — explicit invalidation on write, never a TTL-only guess for data that has a deterministic invalidation point.
- **Batching:** Part 3's Core bulk `insert()` for `api-design.md` §2.10's batch endpoint.
- **Pagination:** cursor-based, aligned with `database-design.md` §6–7's BRIN indexes — shown in full in Part 19.
- **Streaming:** large report/export generation uses an async generator + `StreamingResponse`, never `list(all_rows)` materialized in memory.
- **Database:** async engine pool sized explicitly (`pool_size`, `max_overflow`); a distinctly-named read-replica session factory exists from day one even though it points at the same primary in Phase 1 (`database-design.md` §13 / `system-design.md` §10).

---

# Part 15 — Security Rules

Reference: `security-architecture.md` in full. Implementation checklist:

- [ ] Every query goes through SQLAlchemy's parameterized construct — no `session.execute(text(f"..."))` with interpolated user input, ever (§27)
- [ ] Passwords hashed with `argon2-cffi` (Argon2id), never a fast general-purpose hash (§19)
- [ ] No secret read from a hardcoded value or committed `.env` — always from the secrets manager abstraction (§12)
- [ ] CORS configured to `apps/web`'s origin(s) only — no wildcard (§10)
- [ ] Rate limiting middleware active on `/auth/*` with stricter limits than general API traffic (§31)
- [ ] Every mutating endpoint requires `Idempotency-Key` and checks the audit-log write path (§9's checklist item, this document's Part 6/10)
- [ ] SSRF-guarded outbound HTTP client used for every connector call — never a bare `httpx.get(user_supplied_url)` (§30)
- [ ] File uploads always land in the quarantine bucket first (§24)

---

# Part 16 — Code Review Checklist

Mandatory, every PR:

- [ ] No deep import across a module boundary (Part 1) — `import-linter` passing is necessary but not sufficient; a reviewer still checks intent
- [ ] All business logic is in `service.py`, not `router.py` or `repository.py` (Part 5)
- [ ] Every mutation's outbox write is inside the same transaction as its business write (Part 6)
- [ ] Every new event consumer performs the Inbox claim before any side effect (Part 6)
- [ ] Every new endpoint's response uses `Envelope[T]`, never a bare ORM/dict return (Part 2, 7)
- [ ] Every new domain exception maps to a real `api-design.md` §2.4 code (Part 11)
- [ ] Every new migration has a real `downgrade()` (Part 4)
- [ ] No `TODO`, `FIXME`, `pass  # implement later`, or placeholder left in merged code (Part 17)
- [ ] Tests added at the appropriate layer(s) (Part 13), not only "an end-to-end test exists"
- [ ] Security checklist (Part 15) reviewed for anything the change touches

---

# Part 17 — AI Coding Rules

Rules any AI coding assistant (Claude, Cursor, Copilot, or otherwise) generating code for this repository must follow, without exception:

1. **Never invent an endpoint, event, table, or field not already documented** in `api-design.md`, `event-driven-architecture.md`, or `database-design.md`. If a task requires one that doesn't exist, stop and flag it — do not silently add it to the code without it first existing in the architecture doc.
2. **Never leave a `TODO`, `FIXME`, stub, or `pass`-only function body** in code presented as complete. Incomplete work is stated as incomplete in prose, not hidden in a stub the next reader will mistake for finished.
3. **Never bypass the Unit of Work / outbox pattern** for a mutation that should announce a fact — a "quick" direct `session.execute()` outside a service method's UoW is exactly the kind of shortcut Part 6 exists to prevent.
4. **Never return an ORM model directly from a router.** Always map through a Pydantic response schema, even when it feels redundant for a "simple" endpoint.
5. **Never add a cross-module deep import** to make something "temporarily easier." Route through `public.py` or don't do it.
6. **Never weaken an authorization check** ("just for now," "to unblock testing") without it being an explicit, reviewed, temporary flag clearly labeled as such — never silently.
7. **Always run the project's linter, type checker, and `import-linter` contract** before presenting code as done; a change that would fail CI is not finished.
8. **Always match existing patterns in the module being edited** over introducing a new pattern for the same problem — consistency within this guide takes precedence over a locally "nicer" alternative.
9. **Never fabricate a citation to an architecture document.** If unsure which section governs a decision, say so rather than inventing a plausible-sounding section reference.

**Forbidden shortcuts, explicitly:** skipping the Inbox check "since this handler is simple"; using `SELECT *`-equivalent broad fetches instead of the fields actually needed; catching a bare `except Exception: pass`; disabling a failing test instead of fixing it or the code; hardcoding a value that a config/environment setting should own.

---

# Part 18 — Anti-Patterns

At least one per row; grouped by area. Every one of these has appeared in real backend codebases and is explicitly forbidden here.

**Architecture & module boundaries**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 1 | Deep-importing another module's `models.py`/`repository.py` | Breaks Part 1's boundary and `import-linter`'s contract; defeats Phase 5 extraction readiness |
| 2 | A "utils" or "helpers" module every feature dumps unrelated code into | Becomes an unowned, untestable dependency magnet with no clear boundary |
| 3 | Circular imports "resolved" with a local `import` inside a function | Hides a real design problem instead of fixing it |
| 4 | A module reaching across the DAG (e.g. `ingestion` importing `investigation`) | Violates `database-design.md` §5's acyclic dependency graph |
| 5 | Business logic in `__init__.py` | Makes import order load-bearing and logic hard to find |
| 6 | Sharing a mutable module-level singleton across requests | Breaks under concurrency; use DI-scoped instances instead |
| 7 | A "god service" that orchestrates every other module directly | Recreates a monolith-inside-the-monolith; keep orchestration scoped to the actual use case |
| 8 | Skipping `public.py` because "it's just one function" | Boundary discipline that has exceptions isn't discipline |

**FastAPI**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 9 | Business logic written directly in a router function | Untestable without spinning up HTTP, violates Part 5 |
| 10 | Returning a raw `dict` instead of a `response_model` | No schema validation on the way out, no contract with `api-design.md` |
| 11 | Using `@app.on_event("startup")` instead of `lifespan` | Deprecated, and skips the graceful-shutdown draining `event-driven-architecture.md` §2.2 requires |
| 12 | Catching `Exception` broadly in a router and returning `{"error": str(e)}` | Leaks internals, bypasses Part 11's exception hierarchy entirely |
| 13 | Reading the request body manually instead of a Pydantic model parameter | Loses automatic validation and OpenAPI documentation |
| 14 | A route that both accepts a `case_id` path param and trusts a `case_id` in the body without cross-checking | Opens an authorization confusion bug |
| 15 | Blocking synchronous I/O (`requests.get`, sync DB driver) inside an `async def` route | Blocks the entire event loop for every concurrent request |
| 16 | Global mutable state on `app.state` written from request handlers | Race conditions under concurrent requests |

**SQLAlchemy**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 17 | Raw string-interpolated SQL (`text(f"WHERE id = {id}")`) | SQL injection (`security-architecture.md` §27) |
| 18 | Declaring a `ForeignKey` across two modules' schemas | Violates `database-design.md` §5's no-cross-schema-FK rule |
| 19 | Fetching a full row just to check existence | Wasteful; use `.exists()` |
| 20 | Looping `session.add()` for a large batch instead of bulk `insert()` | Orders of magnitude slower; ignores Part 3's bulk-operations guidance |
| 21 | Relying on implicit lazy-loading working "because it did in sync SQLAlchemy" | Async SQLAlchemy doesn't support it; this is a sign the loading strategy wasn't actually reviewed |
| 22 | Opening a new `AsyncSession` per repository call instead of one per UoW | Breaks transactional atomicity across the use case |
| 23 | Storing a JSONB blob for data that's actually always queried by a specific field | Should be a real, indexed column (`database-design.md` §13) |
| 24 | Forgetting `session.flush()` before using a DB-generated ID later in the same method | `NULL`/stale-ID bugs that only show up under specific ordering |
| 25 | Committing inside a repository method | Repositories don't own transaction boundaries — the UoW does |
| 26 | Using `autoincrement` integer PKs "because it's simpler" | Violates `database-design.md` §4's UUIDv4-everywhere rule, breaks Phase 5 extraction |

**Alembic**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 27 | `downgrade()` left as `pass` | Not actually reversible; a review-blocking defect (Part 4) |
| 28 | One giant migration touching multiple modules' schemas | Violates per-module migration ownership |
| 29 | An unbounded `UPDATE evidence SET ...` in a data migration | Locks a large, high-traffic table; must be batched |
| 30 | A migration that silently drops a column with data in it | Destructive against potentially evidentiary data without an expand/contract sequence |
| 31 | Editing an already-applied, already-shipped migration file | Rewrites history other environments have already run; add a new migration instead |
| 32 | A migration that assumes a specific row exists without checking | Breaks on a fresh database or a different data state |

**Services**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 33 | A service method with no matching use case anyone actually calls | Speculative code (`CLAUDE.md`'s anti-premature-abstraction stance) |
| 34 | Duplicating a validation rule in both the service and the router | Two sources of truth for the same rule, guaranteed to drift |
| 35 | A service that directly imports another module's repository "just this once" | Exactly the shortcut Part 5's orchestration rule forbids |
| 36 | Swallowing a domain exception and returning `None` instead of letting it propagate | Hides a real failure from the caller and from Part 11's error mapping |
| 37 | Putting HTTP-status-code knowledge inside a service | Couples business logic to a transport concern that belongs in Part 11's exception classes |

**Events**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 38 | Publishing an event outside the triggering transaction | Breaks the outbox pattern's atomicity guarantee (Part 6, `event-driven-architecture.md` §16) |
| 39 | A consumer that mutates state before the Inbox claim succeeds | Not idempotent under redelivery |
| 40 | Centralizing all modules' outbox rows in one shared table | Violates per-module ownership (`database-design.md` §2) |
| 41 | An event payload containing the full evidence `attributes` blob | Violates the thin-event-plus-reference policy (`event-driven-architecture.md` §8) |
| 42 | A handler that assumes event delivery order across different aggregates | Only per-aggregate order is guaranteed (`event-driven-architecture.md` §18) |
| 43 | Retrying a failed handler by re-publishing a new event | Should retry the same outbox row's dispatch, not mint a new `event_id` |
| 44 | A new subscription added without a matching catalog entry | Breaks discoverability (`event-driven-architecture.md` §24) and review visibility |
| 45 | Silently dropping a dead-lettered event | Must be explicitly resolved, never deleted (`event-driven-architecture.md` §15) |

**API design violations**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 46 | An endpoint not present in `api-design.md` | This guide's whole premise is "architecture defines WHAT" — implementation doesn't get to add undocumented surface |
| 47 | Returning `404` for an authorization failure inconsistently with the documented ambiguity rule | Breaks the deliberate existence-ambiguity `api-design.md` §2.4 specifies |
| 48 | Offset pagination on a large, append-heavy resource | Contradicts `api-design.md` §2.5's cursor-pagination requirement |
| 49 | A list endpoint without a documented, enforced sort whitelist | Allows sorting on an unindexed or unintended column |
| 50 | Skipping the `Idempotency-Key` check on a documented-as-idempotent mutating endpoint | Silent duplicate creation on client retry |
| 51 | An API response field name that doesn't match `database-design.md`'s column name without a stated reason | Unnecessary translation layer, source of drift |

**Auth & security**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 52 | Storing a session token or secret in a log line | Credential leakage into a system with broad read access |
| 53 | A permission check performed only client-side, trusted server-side | The exact anti-pattern `security-architecture.md` §6/`frontend-architecture.md` §7 both explicitly warn against |
| 54 | Comparing secrets with `==` instead of a constant-time comparison | Timing side-channel |
| 55 | A default admin account with a known/blank password | Textbook initial-access vector |
| 56 | Logging full request bodies indiscriminately | May capture PII/secrets in structured logs |
| 57 | A CORS config with `allow_origins=["*"]` and `allow_credentials=True` | A well-known, severe misconfiguration |
| 58 | Trusting a client-supplied `role` or `user_id` field in a request body | Authorization must come from the verified session, never client input |
| 59 | Disabling TLS certificate verification "for local dev" and leaving it configurable in prod | Exactly the habit `security-architecture.md` §16 explicitly bans |
| 60 | Rolling a custom crypto/hashing routine instead of a vetted library | Custom cryptography is reliably wrong in non-obvious ways |
| 61 | A rate limiter keyed only by IP, not by authenticated actor | Trivially bypassed and unfair to shared-IP legitimate users |

**File handling**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 62 | Loading an entire uploaded file into memory before hashing/scanning | Memory exhaustion risk on large forensic images |
| 63 | Serving a file directly from the quarantine bucket | Violates the "never transiently reachable unscanned" rule (`security-architecture.md` §24) |
| 64 | Trusting a client-supplied `Content-Type` for validation | Trivially spoofable; validate actual content |
| 65 | Deleting a scan-failed file for a forensic-category upload | Violates the domain-aware malware policy (`security-architecture.md` §25) |

**Logging & observability**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 66 | `print()` statements left in place of structured logging | No context, no correlation ID, unusable in production |
| 67 | Writing to `platform.audit_log` from more than one code path | Violates the single-unbypassable-interface rule (`security-architecture.md` §22) |
| 68 | Metrics with unbounded cardinality labels (e.g. raw `user_id` as a label) | Explodes the metrics backend's cardinality |

**Error handling**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 69 | `except Exception: pass` | Silently swallows real failures |
| 70 | Returning `200 OK` with an error message in the body | Breaks every HTTP-status-aware client and monitoring tool |
| 71 | A generic `INTERNAL_ERROR` for a condition that has a real, more specific domain exception | Loses actionable information for both the client and the audit trail |

**Background jobs**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 72 | Forcibly cancelling a job mid-database-transaction | Leaves inconsistent state; cancellation must be cooperative (Part 12) |
| 73 | A job with no persisted status, only in-memory queue state | Loses all progress/status visibility on a worker restart |

**Testing & performance**

| # | Anti-pattern | Why forbidden |
|---|---|---|
| 74 | Mocking the database in a test that's supposed to verify a query's correctness | Tests the mock, not the actual behavior |
| 75 | An `N+1` query left in because "it works in the test with 3 rows" | Works until production data volume, then degrades badly |
| 76 | Disabling a flaky test instead of fixing its root cause | Erodes the value of the whole suite over time |

---

# Part 19 — Examples

Two complete, real vertical slices — every file involved, start to finish, no placeholders, no `TODO`s, no pseudocode. (Part 7 already traced the relationship-review endpoint at the request-flow level; these two are additional, complementary, fully-written slices.)

## Example 1 — Cursor Pagination (`shared/pagination.py`), used by every list endpoint

```python
# sentinelai/shared/pagination.py
import base64
import json
from uuid import UUID

def encode_cursor(sort_value: str, id_value: UUID) -> str:
    raw = json.dumps({"v": sort_value, "id": str(id_value)}).encode()
    return base64.urlsafe_b64encode(raw).decode()

def decode_cursor(cursor: str) -> tuple[str, UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    data = json.loads(raw)
    return data["v"], UUID(data["id"])
```

```python
# sentinelai/shared/envelope.py
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")

class Meta(BaseModel):
    request_id: str
    correlation_id: str

class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta

class Pagination(BaseModel):
    next_cursor: str | None
    has_more: bool
    limit: int

class ListEnvelope(BaseModel, Generic[T]):
    data: list[T]
    pagination: Pagination
    meta: Meta
```

## Example 2 — Full Evidence Ingestion Slice (`POST /api/v1/evidence`, `api-design.md` §5)

```python
# sentinelai/modules/ingestion/schemas.py
from pydantic import BaseModel, Field

class EvidenceCreate(BaseModel):
    schema_version: str
    category: str
    artifact_type: str
    title: str = Field(min_length=1, max_length=200)
    source: dict
    collected_at: datetime
    integrity: dict | None = None
    attributes: dict
    confidence: float = Field(ge=0.0, le=1.0)
    classification: dict

class EvidenceRead(BaseModel):
    model_config = {"from_attributes": True}
    evidence_id: UUID
    schema_version: str
    category: str
    artifact_type: str
    title: str
    status: str
    ingested_at: datetime
```

```python
# sentinelai/modules/ingestion/service.py
class EvidenceIngestionService:
    def __init__(self, uow: UnitOfWork, schema_registry: AttributeSchemaRegistry) -> None:
        self._uow, self._registry = uow, schema_registry

    async def ingest(self, payload: EvidenceCreate, actor: CurrentUser, correlation_id: str) -> Evidence:
        errors = await self._registry.validate(payload.schema_version, payload.category,
                                                 payload.artifact_type, payload.attributes)
        if errors:
            raise ValidationFailedError(details=errors)

        evidence = Evidence(
            evidence_id=uuid4(), schema_version=payload.schema_version, category=payload.category,
            artifact_type=payload.artifact_type, title=payload.title, attributes=payload.attributes,
            status="validated", ingested_at=datetime.now(UTC),
        )
        await self._uow.evidence.add(evidence)
        await self._uow.session.flush()

        await self._uow.custody_events.add(EvidenceCustodyEvent(
            custody_event_id=uuid4(), evidence_id=evidence.evidence_id, sequence_number=1,
            event_type="ingested", occurred_at=datetime.now(UTC), actor_user_id=actor.user_id,
            prev_event_hash=None, entry_hash=_hash_genesis(evidence.evidence_id),
        ))
        await self._uow.outbox.publish(
            event_type="evidence.ingested", aggregate_type="evidence", aggregate_id=evidence.evidence_id,
            payload={"category": evidence.category, "artifact_type": evidence.artifact_type,
                     "collected_at": payload.collected_at.isoformat()},
            correlation_id=correlation_id, actor_type=actor.actor_type, actor_ref=actor.user_id,
        )
        return evidence
```

```python
# sentinelai/modules/ingestion/router.py
@router.post("", response_model=Envelope[EvidenceRead], status_code=201)
async def ingest_evidence(
    payload: EvidenceCreate,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: EvidenceIngestionService = Depends(get_evidence_ingestion_service),
) -> Envelope[EvidenceRead]:
    evidence = await service.ingest(payload, current_user, request.state.correlation_id)
    return Envelope(
        data=EvidenceRead.model_validate(evidence),
        meta=Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id),
    )
```

```python
# tests/integration/test_evidence_ingestion.py
@pytest.mark.asyncio
async def test_ingest_evidence_writes_custody_genesis_and_publishes_event(uow: UnitOfWork, schema_registry):
    service = EvidenceIngestionService(uow, schema_registry)
    payload = EvidenceCreate(
        schema_version="1.2.0", category="mobile_forensics", artifact_type="sms_mms_message",
        title="Test SMS", source={"system": "test", "collector_id": "examiner:test"},
        collected_at=datetime.now(UTC), attributes={"sender": "+1", "recipient": "+2",
        "direction": "outgoing", "body": "hi"}, confidence=1.0,
        classification={"sensitivity": "restricted", "legal_authority_ref": "WARRANT-1"},
    )
    evidence = await service.ingest(payload, make_current_user(role="investigator"), "corr-1")

    assert evidence.status == "validated"
    custody = await uow.custody_events.list_for_evidence(evidence.evidence_id)
    assert custody[0].event_type == "ingested"
    outbox_rows = await uow.session.execute(
        select(get_outbox_table("ingestion")).where(
            get_outbox_table("ingestion").c.aggregate_id == evidence.evidence_id
        )
    )
    assert any(r.event_type == "evidence.ingested" for r in outbox_rows.fetchall())
```

---

# Part 20 — Cross References

| Document | What this guide implements from it |
|---|---|
| [PRD](prd.md) | FR-7.3's human-in-the-loop guarantee → Part 7's relationship-review flow, Part 5's state-machine validation |
| [Architecture](architecture.md) | Module boundaries and `apps/server` structure → Part 1's project layout and public-interface convention |
| [System Design](system-design.md) | §9's tech stack, §12's observability model → Part 10's logging/metrics/tracing |
| [Database Design](database-design.md) | Every table/schema/index referenced by name → Part 3's models, Part 4's per-module Alembic ownership |
| [API Design](api-design.md) | Every endpoint, error code, convention → Part 2, 7, 11's implementations |
| [Event-Driven Architecture](event-driven-architecture.md) | The full event catalog, Outbox/Inbox patterns, retry policies → Part 6, 12 |
| [Canonical Evidence Model](canonical-evidence-model.md) | The Core Evidence Object, validation rules → Part 19's `EvidenceCreate`/`EvidenceIngestionService` |
| [Security Architecture](security-architecture.md) | Auth model, hashing standards, upload security → Part 8, 9, 15 |

---

*This guide is the implementation authority. Any code that conflicts with it is either a bug in the code or a signal this guide needs updating — resolve which, explicitly, rather than letting implementation and guide silently diverge. Update this document in the same change as any architectural document it implements.*
