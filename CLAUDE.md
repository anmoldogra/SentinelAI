# CLAUDE.md

Guidance for Claude Code (and other AI coding agents) working in this repository.

## What this repository is

SentinelAI is an AI-powered Investigation Intelligence Platform spanning five domains: **Digital Forensics, OSINT, Threat Intelligence, Social Media, and Case Management**. It is a monorepo containing multiple deployable apps, shared packages, and infrastructure-as-code.

## Current phase

**Scaffold stage.** As of the initial setup, the repository contains only folder structure, placeholder READMEs, and planning documentation (`docs/vision.md`, `docs/prd.md`, `docs/architecture.md`, `docs/system-design.md`, `docs/canonical-evidence-model.md`, `docs/database-design.md`, `docs/api-design.md`, `docs/event-driven-architecture.md`, `docs/security-architecture.md`, `docs/frontend-architecture.md`, `docs/backend-implementation-guide.md`, `docs/deployment-architecture.md`, `docs/engineering-roadmap.md`, `docs/roadmap.md`). There is no application code yet — the full architecture, implementation-guide, and execution-planning phase is complete; production code has not started.

`docs/canonical-evidence-model.md` is the authoritative design for `packages/evidence-schema` — any work touching evidence ingestion, evidence shape, chain of custody, or the entity/relationship graph must conform to it. If it needs to change, update the design doc in the same change, not just the implementation.

`docs/database-design.md` is the authoritative PostgreSQL data model — schema-per-module ownership, and specifically **no cross-schema foreign key constraints** (cross-module references are unenforced UUID columns validated at the application layer). Any migration work must conform to this and stay inside its module's own schema; do not add a real FK across module schemas even if it seems convenient.

`docs/api-design.md` is the authoritative REST API contract — resource-oriented URLs, the response/error envelope, pagination/idempotency/versioning conventions, and every endpoint's auth, events-published, and audit requirements. It is implementation-independent (no framework assumed). Any `apps/server/entrypoints/http` work must conform to it; if an endpoint's shape needs to change, update this document in the same change, not just the implementation.

`docs/event-driven-architecture.md` is the authoritative spec for everything async — the Outbox/Inbox patterns, the event envelope (`event_id`, `correlation_id`, `causation_id`, `trace_id`, etc.), retry policies, dead-letter handling, and the complete per-module published/consumed event catalog (Section 25). **Two rules from it matter most when writing any module code:** (1) every business write that should announce a fact does so via an outbox insert in the *same transaction* as the write — never a separate publish step; (2) every event handler performs the Inbox check (insert-first on `(event_id, handler_name)`) before any side effect — assume at-least-once delivery always. `apps/server/platform`'s event bus and every module's publish/subscribe code must conform to this document; a new event type or subscription must be added to Section 25's catalog in the same change that introduces it in code.

`docs/security-architecture.md` is the authoritative security reference — it supersedes any security detail stated more briefly elsewhere in this doc series (PRD §9–10, `api-design.md`'s auth model, `database-design.md`'s audit/legal-hold rules). Anything touching authentication, authorization, encryption, secrets/keys, evidence integrity/chain-of-custody enforcement, or upload/storage security must conform to it, not reinvent a lighter version of it. It also carries a standing rule worth internalizing: **never store a session/bearer token in `localStorage` or `sessionStorage`** (§35), **always use parameterized queries, never string-built SQL** (§27), and **any new deletion/purge code path must check `legal_hold` first** (§39).

`docs/frontend-architecture.md` is the authoritative reference for `apps/web` — it assumes React + React Query (TanStack Query) as the UI/server-state libraries (a decision that should be recorded as an ADR before implementation, per its own header note), feature-based folders mirroring `apps/server/modules/*`, and a strict server-state/UI-state/auth-state/URL-state split (§9). **The rule most likely to get violated by accident:** finding/relationship review mutations (`PATCH .../status`) are never optimistic — the UI waits for server confirmation before showing a new disposition, because this platform's human-in-the-loop guarantee (PRD FR-7.3) has to be visible in the UI's actual behavior, not just true on the backend.

`docs/prd.md` is the canonical requirements source (functional/non-functional/security requirements, personas, compliance, MVP scope). If a task involves deciding what a feature should do, check it before inventing behavior — and if code changes what a feature does, update the relevant FR/SR entry in the same change.

`docs/backend-implementation-guide.md` is **the implementation authority for all backend code** — not another architecture document. Every other backend-relevant doc above explains *what*; this one explains *how*, in Python/FastAPI/SQLAlchemy/Alembic/Pydantic v2 specifically, and **no `apps/server` implementation may violate it**. See "Mandatory Implementation Rules" below for the subset that applies to every change, and consult the guide itself (20 parts, including 76 named anti-patterns) before writing any backend code.

`docs/engineering-roadmap.md` is **the master execution plan** — it converts every document above into a scheduled, task-by-task build plan (team structure, phase-by-phase workstreams, every endpoint/module/event/migration/page/component as a task with priority/complexity/dependencies/owner/acceptance criteria, critical path, milestones, technical debt/risk/open-ADR registers). **It is distinct from `docs/roadmap.md`**, which defines the five phases and their exit criteria — `docs/roadmap.md` says *what each phase must achieve*, `docs/engineering-roadmap.md` says *how that gets built and in what order*. When asked to work on a specific task, this is the document to check for its priority, dependencies, and acceptance criteria before starting; when asked what phase the project is in, `docs/roadmap.md` is authoritative.

`docs/deployment-architecture.md` is **the authoritative deployment reference** — Kubernetes manifests, container/networking/storage architecture, database HA, secrets management (Vault + External Secrets Operator), HA/DR, scaling, monitoring, and the four deployment profiles (state/local police, central agency, single-tenant enterprise, future SaaS). It commits to specific infrastructure tooling (CloudNativePG, Harbor, cosign, ArgoCD, the Prometheus/Grafana/Loki/Tempo stack) the same way `docs/backend-implementation-guide.md` commits to Python/FastAPI — treat those as decided, not open, when working on `infra/` or CI/CD. See "Mandatory Deployment Rules" below.

`docs/engineering-governance.md` is **the engineering constitution** — the meta-layer *above* every document listed so far. It governs process, not domain design: the gated SDLC, ADR governance, the mandatory Architecture Quality Gates, code/security/forensics/AI/frontend/API/data/CI-CD standards, the review boards, the universal Definition of Done, and the 5-year roadmap. **It must be consulted before implementing any ADR** — a change is not "done" unless it satisfies its Quality Gates (§3) and the Definition of Done (§13). Where a domain doc says *what* to build it wins on that subject; this manual wins on *how work is governed*.

- Do not add application code, dependencies, or framework boilerplate to `apps/` or `packages/` unless the user explicitly asks for it.
- If a task implies writing code in a module/app that is currently a placeholder, confirm scope with the user first — check `docs/roadmap.md` to see what phase the repo is meant to be in before jumping ahead.

## Architecture: modular monolith, not microservices (for now)

The team is currently one developer, so the domain boundaries below are implemented as **internal modules inside a single deployable (`apps/server`)**, not as separate services — see `docs/architecture.md` "Architectural Style" and `apps/server/README.md` for the full reasoning and the module boundary rules. **Do not suggest or scaffold splitting a module out into its own service/deployable unless the user asks** — that's a Phase 5 decision triggered by an actual team-scale or scaling bottleneck, not something to anticipate by default.

## Repository map

| Path | Contents |
|---|---|
| `apps/web` | Investigator-facing web console (UI) |
| `apps/server` | The modular monolith — single deployable backend, two entrypoints (`entrypoints/http`, `entrypoints/worker`) sharing one codebase |
| `apps/server/modules/ingestion` | Multi-source intake and normalization into the canonical evidence model |
| `apps/server/modules/osint` | OSINT collection connectors and processing |
| `apps/server/modules/threat-intel` | Threat feed consumption, IOC management, correlation |
| `apps/server/modules/forensics` | Digital forensic artifact parsing and chain-of-custody |
| `apps/server/modules/social-media` | Social media monitoring and analysis |
| `apps/server/modules/case-management` | Case lifecycle, evidence linking, chain of custody, reporting |
| `apps/server/modules/investigation` | Core AI orchestration — cross-domain correlation, hypothesis generation, agentic reasoning |
| `apps/server/modules/notification` | Alerting and notifications |
| `apps/server/platform` | Cross-cutting plumbing shared by both entrypoints: db, event bus, auth, HTTP routing |
| `packages/evidence-schema` | Canonical evidence/case data model shared across all domains |
| `packages/shared-types` | Shared TypeScript/type contracts |
| `packages/shared-utils` | Shared utility libraries |
| `packages/ui-components` | Shared UI component library |
| `packages/sdk` | Client SDK for the SentinelAI API |
| `infra/` | Docker, Kubernetes, Terraform |
| `docs/` | Vision, architecture, roadmap, ADRs |
| `.github/CODEOWNERS` | Team ownership mapping (placeholder handles — see `docs/architecture.md` "Team Ownership") |
| `CONTRIBUTING.md` | Branching, commit, and review process |
| `SECURITY.md` | Vulnerability disclosure process |

## Conventions to follow once code lands

- **Bounded contexts stay separate even inside the monolith.** Each module in `apps/server/modules/` owns its own database schema/table namespace; other modules reach it only through its public interface or the `platform` event bus — never by importing its internals or querying its tables directly. This is what keeps extraction cheap later — see `apps/server/README.md` "Module boundary rules".
- **Evidence is immutable and chain-of-custody matters.** Anything touching the `forensics` or `case-management` modules must preserve an audit trail — this is a legal/compliance requirement of the domain, not a nice-to-have.
- **Architecturally significant decisions get an ADR** in `docs/adr/` (see `0001-record-architecture-decisions.md` for the template/rationale). Don't silently make a structural choice (new datastore, new module boundary, new external dependency, or extracting a module into its own service) without recording why.
- **No secrets in the repo.** `.env` files, keys, and credentials are gitignored — use `.env.example` files with placeholder values instead.
- **`apps/server`'s language/framework is decided**: Python + FastAPI + SQLAlchemy 2.0 (async) + Alembic + Pydantic v2, per `docs/backend-implementation-guide.md`'s header note. This resolves what was previously an open Phase 1 blocker in `docs/architecture.md`/`docs/database-design.md` — still worth a formal ADR before implementation, but no longer something to treat as undecided.

## Mandatory Implementation Rules

These apply to every piece of backend code written in this repository, by a human or an AI agent, without exception. They are extracted from `docs/backend-implementation-guide.md`, which has the full reasoning and many more rules (Part 15's security checklist, Part 16's review checklist, Part 18's 76 anti-patterns) — read it before writing non-trivial backend code, not just this summary.

1. **Never invent an endpoint, event, table, or field.** If `docs/api-design.md`, `docs/event-driven-architecture.md`, or `docs/database-design.md` doesn't already document it, stop and flag it — don't add it to code first.
2. **Never leave a `TODO`, `FIXME`, stub, or `pass`-only function** in code presented as complete. State incomplete work as incomplete in prose; don't hide it in a stub.
3. **Every mutation that should announce a fact writes to its module's outbox table in the same database transaction as the business write** — never a separate publish step, never after the commit (`docs/backend-implementation-guide.md` Part 6).
4. **Every event consumer performs the Inbox claim (insert-first on `(event_id, handler_name)`) before any side effect** — assume at-least-once delivery always.
5. **All business logic lives in a service layer** — never in a router/endpoint function, never in a repository. Repositories only persist; routers only parse and delegate.
6. **Never return an ORM model directly from an API route.** Always map through a Pydantic response schema.
7. **Never deep-import another module's internals** (`models.py`, `repository.py`). Cross-module code only goes through that module's `public.py`.
8. **Never bypass or weaken an authorization check** "temporarily" without it being explicit, reviewed, and clearly labeled as such.
9. **Every Alembic migration has a real, working `downgrade()`** — not `pass`.
10. **Run the linter, type checker, and import-boundary check before considering any change done.** A change that would fail CI is not finished.

## Mandatory Deployment Rules

Extracted from `docs/deployment-architecture.md`, which has the full reasoning (24 parts, including hardware sizing, HA/DR, and air-gapped procedures) — read it before touching `infra/`, Kubernetes manifests, or CI/CD deployment steps.

1. **No manual `kubectl apply` (or equivalent) against a real environment.** Every infrastructure change is a reviewed, merged Git change that ArgoCD reconciles — GitOps, not imperative changes, even for a "quick fix."
2. **Every container image is signed (cosign) before it can be deployed**, and only images built from an approved base image (`docs/deployment-architecture.md` Part 5's table) are permitted.
3. **No secret is ever placed in a `ConfigMap`, a committed manifest, or an environment variable set by hand.** Secrets flow from Vault through the External Secrets Operator only.
4. **Every namespace gets a default-deny `NetworkPolicy` plus explicit, minimal allow rules** — never an open-by-default namespace.
5. **Every Alembic migration Job runs as an ArgoCD `PreSync` hook, in module-DAG order** (`platform` → `ingestion` → domain modules → `investigation` → `notification`) — never applied out of order or by hand against production.
6. **Air-gapped deployments must have zero configured or observed egress paths**, including DNS forwarders — verify, don't assume.
7. **Every release ships with a validated rollback path** (an ArgoCD revert exercised in staging) before it's trusted in production.
8. **Autoscaling bounds (`minReplicas`/`maxReplicas`) are always set deliberately** against the database's actual `max_connections` ceiling — never left at a default that could exhaust the connection pool under scale-up.

## Working style

- Prefer editing/extending existing structure over introducing new top-level folders — propose the change and confirm before restructuring.
- Keep documentation and code in sync: if an architectural decision changes what's in `docs/architecture.md`, update that file in the same change.
