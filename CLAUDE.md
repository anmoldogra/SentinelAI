# CLAUDE.md

Guidance for Claude Code (and other AI coding agents) working in this repository.

## What this repository is

SentinelAI is an AI-powered Investigation Intelligence Platform spanning five domains: **Digital Forensics, OSINT, Threat Intelligence, Social Media, and Case Management**. It is a monorepo containing multiple deployable apps, shared packages, and infrastructure-as-code.

## Current phase

**Scaffold stage.** As of the initial setup, the repository contains only folder structure, placeholder READMEs, and planning documentation (`docs/vision.md`, `docs/architecture.md`, `docs/roadmap.md`). There is no application code yet.

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
- **One decision blocks Phase 1 and must not be quietly assumed**: `apps/server`'s language/framework (see `docs/architecture.md` Open Questions). If a task depends on it, flag it rather than picking one unilaterally.

## Working style

- Prefer editing/extending existing structure over introducing new top-level folders — propose the change and confirm before restructuring.
- Keep documentation and code in sync: if an architectural decision changes what's in `docs/architecture.md`, update that file in the same change.
