# Architecture

> This document describes the intended system architecture for SentinelAI. The repository is currently in its scaffold phase — the structure below is the target shape, not yet-implemented fact. Update this document as real architectural decisions are made (and record significant ones in `docs/adr/`). For the detailed technical design (data flow, event contracts, scalability, fault tolerance, observability, deployment, diagrams), see [System Design](system-design.md).

## Architectural Style

SentinelAI is a **monorepo organized around domain-bounded contexts** — the domain boundaries are fixed from day one, but how those boundaries are *deployed* changes with the team's size.

```
apps/        →  what users and external systems talk to
packages/    →  shared contracts and utilities, no business logic of their own
infra/       →  how it all gets deployed and run
```

**Phase 1 (current — single developer): modular monolith.** All eight domain modules live inside one deployable, `apps/server`, as internal modules with enforced boundaries (own schema, communicate only through public interfaces/events — see `apps/server/README.md`). One codebase, one database, one deploy.

**Why not microservices yet:** with one developer, a network hop between every domain call, eight independent deploys, and distributed transactions across services are pure overhead — they exist to solve team-coordination and independent-scaling problems that don't exist yet. A modular monolith gets the same domain isolation (so the design doesn't get worse over time) without that overhead. See `apps/server/README.md` for the full reasoning and the enforced module rules.

**Later (team/scale-driven): microservices.** As team size or independent-scaling needs justify it, individual modules are extracted into standalone services using the recipe in `apps/server/README.md` — because the module boundary rules are enforced from Phase 1, extraction is a folder move plus an adapter swap, not a redesign. This document's Domain Boundaries table below describes the target end-state each module extracts *toward*; `apps/server/README.md` describes the Phase 1 reality and the mechanics of getting from one to the other.

## Domain Boundaries (bounded contexts)

Each domain below owns its own data and is only reachable through its public interface/events — in Phase 1 that boundary is enforced by convention within `apps/server`; post-extraction it's enforced by the network.

| Domain | Owns | Phase 1 location | Target service |
|---|---|---|---|
| Ingestion | Intake, validation, normalization into the canonical evidence model | `apps/server/modules/ingestion` | `ingestion-service` |
| OSINT | Open-source collection connectors, source reliability scoring | `apps/server/modules/osint` | `osint-service` |
| Threat Intelligence | IOC management, feed correlation, threat actor context | `apps/server/modules/threat-intel` | `threat-intel-service` |
| Digital Forensics | Artifact parsing, chain-of-custody, forensic metadata | `apps/server/modules/forensics` | `forensics-service` |
| Social Media | Monitoring, content capture, account/network analysis | `apps/server/modules/social-media` | `social-media-service` |
| Case Management | Case lifecycle, evidence linking, chain of custody, reporting | `apps/server/modules/case-management` | `case-management-service` |
| Investigation (AI) | Cross-domain correlation, hypothesis generation, agentic reasoning | `apps/server/modules/investigation` | `investigation-engine` |
| Notifications | Alerting, delivery (email/Slack/webhook) | `apps/server/modules/notification` | `notification-service` |

`investigation` is the only module designed to read across domains — it consumes evidence and case data from the others (via their public interfaces/events, never their internal storage) to perform cross-domain AI correlation. This keeps each domain's data model independent while still enabling the platform's core value: connecting evidence across sources. It's also, deliberately, the last module worth extracting — see its extraction note in `apps/server/modules/investigation/README.md`.

## Data Flow (high level)

1. Raw data enters through the `ingestion` module or a domain-specific module (`osint`, `social-media`, `forensics`, `threat-intel`).
2. It is normalized into the **canonical evidence model** (`packages/evidence-schema`) and persisted with source, timestamp, and integrity metadata.
3. Evidence is linked to a case by the `case-management` module, preserving chain of custody.
4. `investigation` consumes evidence across modules to surface correlations, generate hypotheses, and propose leads — always attributed back to source evidence.
5. Analysts review AI output through `apps/web`, accept/reject/annotate findings, and `case-management` records the outcome.
6. `notification` alerts relevant analysts on significant events (new correlation, case status change, SLA breach).

In Phase 1 every step above after "raw data enters" is an in-process call or in-process event (see `apps/server/platform/README.md`'s event bus) — there is no network hop in this flow until a module is extracted.

## Team Ownership

This is currently a team of one — the model below is the target once the team grows, kept here so module boundaries (which already match it) don't need to be redrawn later. See `.github/CODEOWNERS` for the enforced mapping, and note its handles are placeholders until real teams exist.

| Squad | Owns |
|---|---|
| Product & Platform | `apps/web`, `apps/server/entrypoints`, `notification` module, shared packages (`ui-components`, `sdk`, `shared-types`, `shared-utils`) |
| Collection | `ingestion`, `osint`, `threat-intel`, `social-media` modules — everything that pulls external data in |
| Casework | `forensics`, `case-management` modules — grouped together because both carry chain-of-custody/legal obligations |
| AI Investigation | `investigation` module, `evidence-schema` — the cross-domain correlation core and the contract it depends on |
| Platform & Infrastructure | `infra/`, `.github/`, `docker-compose.yml`, `apps/server/platform` |

`docs/`, `CLAUDE.md`, `CONTRIBUTING.md`, and ADRs are owned by a cross-squad architecture guild once one exists — architecturally significant changes shouldn't be approvable by a single squad in isolation.

## Non-Functional Requirements

- **Auditability**: every write to evidence or case state is attributable and immutable (append-only history).
- **Chain of custody**: forensic and case evidence must preserve an unbroken, verifiable custody trail — a legal, not just technical, requirement. This holds regardless of monolith vs. microservice; it's enforced by the module's own schema/audit log, not by deployment topology.
- **Explainable AI output**: AI-generated correlations/hypotheses must reference the source evidence they were derived from.
- **Security**: evidence often includes sensitive/PII data — encryption at rest and in transit, least-privilege access, and full access logging are baseline requirements, not later additions.
- **Scalability**: ingestion volume (especially OSINT/social media) can spike. In Phase 1 this is handled by scaling `apps/server/entrypoints/worker` replicas; true independent scaling arrives when `ingestion`/`osint`/`social-media` are extracted per the recipe in `apps/server/README.md`.

## Tech Stack

| Layer | Choice | Phase 1 status |
|---|---|---|
| Relational store | PostgreSQL | active — one instance, one schema per module |
| Cache / queue backend | Redis | active |
| Object storage | S3-compatible (MinIO locally, S3 in production) | active |
| Event streaming | Kafka-compatible (Redpanda) | deferred — in-process event bus used until a module is extracted or async volume genuinely needs a durable queue |
| Vector store (AI/RAG) | Qdrant | deferred — not needed until `investigation` does AI/RAG work (Phase 3) |
| Container orchestration | Kubernetes | deferred — single-container deploy is sufficient at current scale |
| IaC | Terraform | deferred |

Deferred items are still provisioned (commented out) in `docker-compose.yml` so turning them on is uncommenting, not redesigning. Framework/language choice for `apps/server` itself is intentionally not yet fixed — see Open Questions.

## Open Questions

- **`apps/server` language/framework** (blocking Phase 1): needs an ADR before module implementation starts — affects how module-boundary enforcement (rule 1 in `apps/server/README.md`) gets tooled (e.g. a dependency-boundary lint rule).
- Multi-tenancy model: single-tenant deployments vs. shared platform with tenant isolation?
- AI model strategy: hosted LLM API vs. self-hosted models, and how that choice interacts with handling sensitive evidence.
- Identity/auth strategy for `apps/server/entrypoints/http` (see roadmap Phase 1).
- Secrets management: `.env.example` conventions are enough for scaffold-stage local dev, but production needs a real secrets manager (Vault, cloud-native equivalent) — decide alongside `infra/terraform`.
- **Deferred, not forgotten**: monorepo build/test orchestration across polyglot services, and per-service release/versioning strategy — irrelevant while there's one deployable, but must be resolved before the first module extraction happens.

Track decisions on these as ADRs in `docs/adr/` once made.
