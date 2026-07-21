# SentinelAI

**An AI-powered Investigation Intelligence Platform for Digital Forensics, OSINT, Threat Intelligence, Social Media, and Case Management.**

SentinelAI unifies evidence from forensic artifacts, open-source intelligence, threat feeds, and social media into a single, AI-assisted investigation workflow — helping analysts correlate signals, generate leads, and produce defensible, auditable case reports faster than manual triage allows.

> **Status: Scaffold stage.** This repository currently contains architecture, documentation, and folder structure only. No application code has been written yet. See [docs/roadmap.md](docs/roadmap.md) for what's next.

## Repository Layout

This is a monorepo. Each top-level folder — and every app/module/package within it — has its own `README.md` explaining its purpose in detail.

> **Architecture note:** with a single developer on the project, the backend is a **modular monolith** (`apps/server`), not eight separate microservices — same domain boundaries, one deployable. See [docs/architecture.md](docs/architecture.md) for why, and [apps/server/README.md](apps/server/README.md) for how each module can later be extracted into its own service without a redesign.

| Folder | Purpose |
|---|---|
| [`apps/web`](apps/web/) | Investigator-facing web console (UI) |
| [`apps/server`](apps/server/) | The backend — a modular monolith with one module per investigation domain (ingestion, OSINT, threat intel, forensics, social media, case management, AI investigation engine, notifications) |
| [`packages/`](packages/) | Shared libraries consumed by `apps/` (canonical evidence schema, types, utils, UI components, SDK) |
| [`infra/`](infra/) | Infrastructure as code (Docker, Kubernetes, Terraform) |
| [`docs/`](docs/) | Product vision, architecture, roadmap, and architecture decision records |
| [`scripts/`](scripts/) | Developer and operational tooling scripts |
| [`tests/`](tests/) | Cross-cutting end-to-end and integration test suites |
| [`.github/workflows/`](.github/workflows/) | CI/CD pipeline definitions |

## Documentation

- [Vision](docs/vision.md) — what SentinelAI is and why it exists
- [Product Requirements Document](docs/prd.md) — the canonical requirements: personas, functional/non-functional/security requirements, compliance, MVP scope, success metrics
- [Architecture](docs/architecture.md) — architectural style, principles, team ownership, and tech stack
- [System Design](docs/system-design.md) — the technical deep-dive: data flow, event contracts, scalability, fault tolerance, observability, deployment topology, and diagrams
- [Canonical Evidence Model](docs/canonical-evidence-model.md) — the single data shape every evidence source normalizes into: core object, chain of custody, categories, entities/relationships, validation rules
- [Database Design](docs/database-design.md) — the PostgreSQL data model: schema ownership, tables, keys, indexing, partitioning, versioning, audit, migration, and backup strategy
- [API Design](docs/api-design.md) — the complete REST API contract: every endpoint per module, conventions (pagination, errors, idempotency, versioning), and the Evidence/Investigation/Report/Notification/Auth API deep-dives
- [Event-Driven Architecture](docs/event-driven-architecture.md) — the complete asynchronous messaging spec: the Outbox/Inbox patterns, the event envelope, correlation/causation/trace IDs, retry/DLQ/replay strategy, and the full per-module event catalog
- [Security Architecture](docs/security-architecture.md) — the authoritative security reference: threat model, zero trust, auth/authz, encryption/key management, evidence integrity, application security, air-gapped deployment, supply chain, and incident response
- [Frontend Architecture](docs/frontend-architecture.md) — the authoritative `apps/web` reference: feature-based structure, routing, state/React Query, forms, the Investigation/Evidence Explorer/Entity Graph UIs, accessibility, and performance
- [Backend Implementation Guide](docs/backend-implementation-guide.md) — **the implementation authority for `apps/server`**: Python/FastAPI/SQLAlchemy/Alembic/Pydantic v2 coding standards, the Unit of Work and Outbox/Inbox patterns in code, testing strategy, AI coding rules, and 76 named anti-patterns. Architecture explains WHAT; this explains HOW — no implementation may violate it.
- [Deployment Architecture](docs/deployment-architecture.md) — **the authoritative deployment reference**: Kubernetes/container/networking/storage architecture with real manifests, database HA and migration ordering, secrets/config management, HA and disaster recovery, scaling, monitoring (Prometheus/Grafana/Loki/Tempo), air-gapped deployment, the four deployment profiles (state/local, central agency, single-tenant, future SaaS), and hardware sizing tiers
- [Engineering Roadmap](docs/engineering-roadmap.md) — **the master execution plan**: converts the entire architecture series into a scheduled, task-by-task build plan — team structure, phase-by-phase workstreams, every API endpoint/module/event/migration/page/component broken into tasks with priority/complexity/dependencies/owner/acceptance criteria, critical path, milestones, and the technical debt/risk/open-ADR registers. Architecture explains WHAT; this explains WHEN and BY WHOM.
- [Roadmap](docs/roadmap.md) — phased delivery plan
- [CLAUDE.md](CLAUDE.md) — guidance for AI coding agents working in this repo
- [CONTRIBUTING.md](CONTRIBUTING.md) — branching, commit, and review process
- [SECURITY.md](SECURITY.md) — how to report a vulnerability
- [.github/CODEOWNERS](.github/CODEOWNERS) — who owns what

## Getting Started

Local development instructions will be added once Phase 1 (foundations) implementation begins. For now, `docker-compose.yml` provisions the baseline infrastructure dependencies (database, cache, event stream, object storage, vector store) that upcoming services will rely on:

```bash
docker compose up -d
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branching, commit conventions, and review process, and [.github/CODEOWNERS](.github/CODEOWNERS) for who reviews what. Coordinate architecturally significant changes via `docs/adr/`.

## License

[MIT](LICENSE)
