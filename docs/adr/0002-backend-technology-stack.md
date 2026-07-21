# 2. Backend Technology Stack (apps/server)

## Status

Accepted

## Context

`apps/server` needs a concrete language and framework stack before implementation
can begin. `docs/roadmap.md` Phase 0 lists this as a blocking decision, and
`docs/architecture.md` carried it as an open question. `docs/backend-implementation-guide.md`
and `docs/deployment-architecture.md` were subsequently written assuming a
specific stack; this ADR records that decision formally so code is not ahead of
a recorded choice, per the ADR discipline in [0001](0001-record-architecture-decisions.md).

The backend is a **modular monolith** (`apps/server/README.md`) with two
entrypoints (`entrypoints/http`, `entrypoints/worker`) sharing one codebase, and
must satisfy: async I/O throughout (evidence ingestion, external connectors),
the Outbox/Inbox event patterns, per-module Postgres schemas with isolated
Alembic environments, and strict typed contracts at every boundary.

## Decision

The `apps/server` stack is:

- **Python 3.12** — single implementation language for the backend.
- **FastAPI** — HTTP framework (`entrypoints/http`), one `APIRouter` per module.
- **SQLAlchemy 2.0 (async)** with **asyncpg** — persistence. **No synchronous
  database access anywhere.**
- **Alembic** — migrations, one environment per module with a per-schema
  `version_table_schema` (`docs/database-design.md`).
- **Pydantic v2** + **pydantic-settings** — request/response schemas and config.
- **structlog** — structured logging.
- **arq** + **Redis** — background jobs / scheduled work (`entrypoints/worker`).
- **argon2-cffi** — password hashing; **PyJWT** — token handling.
- **prometheus-client** — metrics endpoint.
- Tooling: **ruff** (lint+format), **mypy** (strict), **import-linter**
  (module dependency DAG), **pytest** + **pytest-asyncio**.

Explicitly **excluded**: Django, Flask, Celery, SQLModel, MongoDB, GraphQL, and
any synchronous DB driver.

## Consequences

- `docs/backend-implementation-guide.md` becomes directly actionable; its code
  examples are normative, not illustrative.
- The async-everywhere rule removes a class of blocking-I/O bugs but requires all
  libraries touching I/O to have async support (enforced in review).
- import-linter encodes the `database-design.md` §5 dependency DAG as a CI gate,
  making module-boundary violations a build failure rather than a review catch.
- Extraction to microservices (Phase 5) stays a mechanical move: each module's
  schema, Alembic env, and public interface are already isolated.
- Revisiting any single library later is a new ADR superseding this one, not an
  edit here.
