# Roadmap

Dates are intentionally omitted — phases are sequenced, not scheduled, until Phase 1 kicks off and real estimates can be made.

## Phase 0 — Scaffold (current)

- [x] Monorepo folder structure
- [x] Vision, architecture, and roadmap documentation
- [x] Local infrastructure baseline (`docker-compose.yml`)
- [x] Contributor/agent guidance (`CLAUDE.md`)
- [x] Team-scale governance: `CODEOWNERS`, `CONTRIBUTING.md`, `SECURITY.md`, PR/issue templates, baseline PR-validation CI (currently aspirational — see `docs/architecture.md` "Team Ownership"; this is a team of one today)
- [x] Repository restructured as a modular monolith (`apps/server`) for Phase 1 implementation speed — see `docs/architecture.md` "Architectural Style"
- [ ] ADR: `apps/server` language/framework (blocking — see `docs/architecture.md` Open Questions)
- [ ] First ADRs recording foundational decisions (auth strategy, multi-tenancy)

## Phase 1 — Foundations

- Authentication & authorization (analyst identity, roles, audit logging baseline)
- `packages/evidence-schema`: canonical evidence and case data model
- `apps/server/modules/case-management`: minimal case CRUD, evidence linking, chain-of-custody log
- `apps/server/modules/ingestion`: generic intake API + normalization pipeline (no source connectors yet)
- `apps/server/entrypoints/http`: routing, auth enforcement, request/response contracts
- `apps/web`: bare-bones console — create/view cases, view linked evidence

**Exit criteria:** an analyst can create a case, manually ingest a piece of evidence, and see it attached to the case with a recorded chain of custody — no AI involved yet.

## Phase 2 — Domain Connectors

- `apps/server/modules/osint`: first OSINT source connector(s)
- `apps/server/modules/threat-intel`: first threat feed integration + IOC storage
- `apps/server/modules/forensics`: first forensic artifact parser
- `apps/server/modules/social-media`: first platform connector
- Evidence from each module flows into the canonical model and attaches to cases

**Exit criteria:** evidence from at least two distinct domains can be ingested into the same case.

## Phase 3 — AI Investigation Engine (MVP)

- `apps/server/modules/investigation`: cross-domain correlation over ingested evidence
- AI-generated hypotheses/leads, each attributed to source evidence
- Analyst review workflow (accept/reject/annotate AI findings) in `apps/web`
- `apps/server/modules/notification`: alert analysts on new correlations

**Exit criteria:** the platform surfaces at least one non-obvious cross-domain correlation an analyst confirms is useful.

## Phase 4 — Enterprise Hardening

- Multi-tenancy (per architecture decision from Phase 0/1)
- Fine-grained RBAC and full audit trail export (for legal/regulatory review)
- Formal report generation/export from `apps/server/modules/case-management`
- SDK (`packages/sdk`) for external/programmatic integration
- Team grows beyond one developer: activate the squad model in `docs/architecture.md` "Team Ownership", replace `.github/CODEOWNERS` placeholder handles with real teams

## Phase 5 — Service Extraction (as needed, not on a fixed schedule)

Triggered by an actual bottleneck (team coordination or independent-scaling need), not by calendar time — see `docs/architecture.md` "Architectural Style":

- Apply the extraction recipe in `apps/server/README.md` to the module(s) under pressure
- Re-enable Redpanda in `docker-compose.yml` for the extracted module's event traffic
- Horizontal scaling of ingestion/correlation paths independent of the rest of the platform

## Beyond

- Additional domain connectors (as new evidence sources are prioritized)
- Advanced agentic investigation workflows (multi-step autonomous research with analyst checkpoints)
- Integrations marketplace / plugin model for third-party data sources
