# SentinelAI Engineering Governance & Development Standards

> **Status:** Ratified engineering constitution. **This document is authoritative over process,
> governance, and quality; it does not restate domain design.** It sits *above* the doc series and
> *binds* it: `vision.md`, `prd.md`, `roadmap.md`, `engineering-roadmap.md`, `architecture.md`,
> `system-design.md`, `security-architecture.md`, `canonical-evidence-model.md`,
> `database-design.md`, `api-design.md`, `event-driven-architecture.md`,
> `backend-implementation-guide.md`, `frontend-architecture.md`, `deployment-architecture.md`, and
> every ADR in `docs/adr/`. Where a domain document specifies *what* to build, it wins on that
> subject; this manual specifies *how work is governed* and wins on process.
>
> **Referencing rule:** No ADR may be marked Accepted, and no subsystem may enter implementation,
> without demonstrating conformance to the Quality Gates (§3) and Definition of Done (§13) below.
>
> **Horizon:** every rule here is chosen to keep SentinelAI maintainable, secure, scalable, and
> forensically trustworthy for a **15–20 year** multi-agency, court-admissible deployment.
> Architecture quality outranks development speed. When in doubt, fail closed and write it down.

## 0. Governing principles (the non-negotiables)

1. **Evidence is sacred.** Anything that touches evidence is immutable, hash-verified,
   custody-tracked, legal-hold-aware, and court-defensible — or it does not ship.
2. **Fail closed.** Absent a positive security/integrity signal, deny, refuse, or halt. Never
   default to trust, never default to open.
3. **Everything auditable.** Every state change that matters produces an immutable, attributable
   record. If it isn't audited, it didn't happen defensibly.
4. **Decisions are written down.** Architecturally significant choices are ADRs; undocumented
   structural decisions are defects.
5. **Boundaries are real.** Module/bounded-context boundaries are enforced by tooling, not
   goodwill. Cross-boundary access goes through a published contract or the event bus — never
   internals.
6. **Design for the successor.** Assume every author leaves and every tool is replaced within the
   horizon. Optimize for the engineer reading this in 2039.
7. **Human-in-the-loop for consequential judgement.** AI assists; a qualified human decides
   anything that affects a person, a case disposition, or evidence.
8. **Reversibility.** Every change ships with a validated rollback; every deployment is GitOps and
   revertible.

---

## 1. Software Development Lifecycle (SDLC)

The lifecycle is a **gated pipeline**; a work item may not advance to the next stage until the
current stage's exit criteria are signed off. Gates marked 🔒 are hard stops (a named board or
owner must approve).

| # | Stage | Purpose | Primary artifact | Exit criteria |
|---|---|---|---|---|
| 1 | **Vision alignment** | Confirm the idea serves the mission & a persona | Problem statement linked to `vision.md`/`prd.md` | Traces to an FR/NFR/SR or a new one is drafted |
| 2 | **Requirements** | Define behavior, not solution | FR/NFR/SR entries in `prd.md` | Testable acceptance criteria exist |
| 3 | **ADR creation** | Record the decision (§2) | ADR in `docs/adr/` | Draft ADR with options + consequences |
| 4 | 🔒 **Architecture Review** | Structural soundness | ARB sign-off (§12) | Passes applicable Quality Gates (§3) |
| 5 | 🔒 **Security Review + Threat Model** | Adversarial soundness | Threat model + SRB sign-off | STRIDE/abuse cases covered; residuals accepted in writing |
| 6 | **Implementation** | Build to the guide | Code conforming to `backend-implementation-guide.md`/§4 | Compiles, self-reviewed, no TODO/stub-as-done |
| 7 | **Unit testing** | Prove units | Tests + coverage report | Coverage floor met (§4); invariants tested |
| 8 | **Integration testing** | Prove seams | Contract/integration tests | Cross-module contracts + event flows green |
| 9 | **Performance testing** | Prove it scales | Benchmark regression suite | Meets NFR budgets; no regression vs. baseline |
| 10 | 🔒 **Code review** | Peer correctness + standards | ≥1 approving reviewer (2 for security/evidence/crypto) | Review checklist (guide Part 16) satisfied |
| 11 | 🔒 **Architecture validation** | Built == designed | Import-boundary + ADR-conformance check | No boundary violations; matches the ADR |
| 12 | **Independent audit** *(for Critical subsystems)* | Reject-first adversarial review | Audit report + scored verdict | No open Critical findings |
| 13 | **Documentation** | Keep docs == code | Updated docs + ADR + runbook | Doc-sync check green (§13) |
| 14 | 🔒 **Production Readiness Review** | Operable & recoverable | PRR checklist (§12) | Rollback, observability, runbooks, DR verified |
| 15 | 🔒 **Deployment** | GitOps release | ArgoCD-reconciled change | Signed image, migrations DAG-ordered, canary green |
| 16 | **Continuous improvement** | Learn | Post-release review / incident retro | Actions logged to debt/risk registers |

**Subsystem criticality tiers** (set the rigor): **Tier 0 — Evidentiary/Crypto/Auth** (all gates
+ independent audit, 2 reviewers, mandatory threat model); **Tier 1 — Domain modules & public
APIs** (all gates, 1–2 reviewers); **Tier 2 — Internal tooling/UI-only** (lightweight: gates 4/5
may be a checklist sign-off). Tier is declared in the ADR and cannot be lowered without ARB
approval.

---

## 2. ADR Governance

- **Location & numbering.** `docs/adr/NNNN-kebab-title.md`, zero-padded, monotonic, never reused.
  `0001` establishes the process. Superseded numbers are retired, not recycled.
- **Template** (mandatory sections): Title · Status · Context · Decision · **Criticality Tier** ·
  **Quality Gates checklist** (§3) · Threat Model (Tier 0/1) · Consequences · Alternatives
  considered · Migration/rollback · **Supersedes/Superseded-by** · Review sign-offs.
- **Status lifecycle:** `Proposed → Under Review → Accepted → (Implemented) → (Deprecated |
  Superseded)`. A revision that changes the decision **rewrites the ADR clean and supersedes** —
  it never silently appends (precedent: ADR-0009 revision).
- **Decision ownership.** Every ADR has one accountable **owner** (drives it to a verdict) and a
  **domain steward** (the board chair for its area). Ownership is recorded and transferable.
- **Approval workflow.** Draft → ARB review → domain board(s) review (Security/Forensics/AI/Perf
  as applicable) → owner records verdict. **Required reviewers by area:** Tier 0 needs ARB + SRB +
  (DFRB if evidentiary) + (AIRB if AI); Tier 1 needs ARB + the one most-relevant domain board;
  Tier 2 needs ARB or a delegated senior reviewer.
- **Versioning.** ADRs are versioned by supersession, not in-place edits to the Decision. Editorial
  fixes are allowed; decision changes require a new/superseding ADR with a fresh audit for Tier 0.
- **Deprecation & successors.** A superseding ADR names its predecessor in `Supersedes:`; the
  predecessor is marked `Superseded-by:` and left in place for historical traceability. Never
  delete an ADR.
- **Traceability.** Every ADR links **up** to the FR/NFR/SR it satisfies and **down** to the
  modules/endpoints/events/migrations it governs. Code that implements an ADR references it in the
  module README; CI's ADR-compliance check (§11) fails a PR that implements an undocumented
  decision.

---

## 3. Architecture Quality Gates

Every ADR carries a **Gate Checklist**; each applicable gate is `Pass / N-A (justified) / Fail`.
A single `Fail` blocks acceptance. `N-A` requires a one-line justification.

| Gate | Question it answers | Owning board |
|---|---|---|
| **Clean Architecture** | Do dependencies point inward to abstractions? No framework leakage into domain? | ARB |
| **DDD compliance** | Is the bounded context correct; is boundary access via contract/event only? | ARB |
| **SOLID** | SRP/OCP/LSP/ISP/DIP honored at the seam being added? | ARB |
| **CQRS suitability** | Is command/query separation warranted here, or is it over-engineering? | ARB |
| **Event-sourcing suitability** | Is the source of truth state or events? Outbox/Inbox correctly applied? | ARB |
| **Security review** | STRIDE covered; least privilege; fail-closed; no secret in repo/config/log? | SRB |
| **Forensic integrity** | Immutability, hash-chain, custody, legal-hold, admissibility preserved? | DFRB |
| **Scalability** | Meets throughput/latency NFRs; no shared bottleneck; horizontal path exists? | PRB |
| **Maintainability** | Will a new engineer understand and change this safely in 5 years? | ARB |
| **Cloud readiness** | Twelve-factor; GitOps; stateless where possible; profile-portable? | ARB |
| **Disaster recovery** | RPO/RTO defined; backup + restore drilled; failure modes recover? | PRB |
| **AI governance** | Explainable, HITL where consequential, confidence-scored, auditable? | AIRB |
| **Privacy** | Data-minimization, purpose limitation, retention, subject rights honored? | SRB |
| **Compliance** | Meets the target certifications' controls (§14); evidence retained? | SRB |

Gate applicability is by tier and domain: a UI-only change need not pass Forensic Integrity;
anything touching evidence **must**.

---

## 4. Code Quality Standards

**Universal rules (all languages):** boundaries enforced by tooling; no cross-module deep imports;
no secrets in code; no `TODO`/`FIXME`/stub presented as done; structured logging only (never
`print`/`console.log`); errors are typed and never swallowed; every public function documented at
the contract level. **Coverage floors:** Tier 0 ≥ 90% + 100% of security/evidentiary invariants
explicitly tested; Tier 1 ≥ 80%; Tier 2 ≥ 60%. Coverage is a floor, not a goal — invariant tests
matter more than the number.

### Backend — Python / FastAPI / SQLAlchemy / PostgreSQL

Authority: `backend-implementation-guide.md` (Python 3.12+, FastAPI, SQLAlchemy 2.0 async,
Alembic, Pydantic v2). This section states the governance subset; the guide's 76 anti-patterns and
checklists are binding in full.

- **Naming:** `snake_case` modules/functions, `PascalCase` classes, `UPPER_SNAKE` constants;
  schemas suffixed by role (`...Create`, `...Update`, `...Response`); no abbreviations that aren't
  domain terms.
- **Folder structure & module boundaries:** `apps/server/src/sentinelai/modules/<module>/` owns
  `router.py` (parse+delegate only), `service.py` (all business logic), `repository.py` (persist
  only), `models.py`, `schemas.py`, `events.py`, `jobs.py`, `public.py`. Cross-module code imports
  **only** `<module>/public.py` or uses the event bus — never `models.py`/`repository.py`.
- **Dependency rules:** domain modules depend on `platform`, never on each other's internals;
  `platform.crypto` imports no module; enforced by import-linter in CI.
- **Persistence:** parameterized queries only (never string-built SQL); schema-per-module; **no
  cross-schema FK** (unenforced UUIDs validated in the app layer); every migration has a real
  `downgrade()`.
- **Async/events:** every fact-announcing write inserts to the module outbox **in the same
  transaction**; every consumer does the Inbox claim before any side effect.
- **API:** never return an ORM model — map through a Pydantic response schema; conform to
  `api-design.md`'s envelope, pagination, idempotency, versioning.
- **Logging/errors:** `structlog`; correlation/trace IDs propagated; no key material or PII in
  logs; exceptions are typed and mapped to the API error contract.

### Frontend — React / TypeScript

Authority: `frontend-architecture.md` (React + TanStack Query). Governance subset:

- **Naming/structure:** feature folders mirroring `modules/*`; `PascalCase` components,
  `camelCase` hooks (`useX`); strict TypeScript, `noImplicitAny`, no `any` without a justified
  `// eslint-disable` and reviewer sign-off.
- **State split:** server-state (React Query) / UI-state / auth-state / URL-state kept strictly
  separate (§8).
- **Boundary rule (must not be violated):** finding/relationship review mutations are **never
  optimistic** — the UI waits for server confirmation (PRD FR-7.3 human-in-the-loop must be
  visible in behavior).
- **Security:** never store a session/bearer token in `localStorage`/`sessionStorage`.
- **Docs/logging/errors:** component contracts documented in the component library; error
  boundaries around every route; no `console.log` in committed code.

### Infrastructure — Docker / Kubernetes / Terraform

Authority: `deployment-architecture.md`. Governance subset:

- Only approved, **signed (cosign)** base images; minimal, non-root, read-only-rootfs where
  feasible; pinned digests.
- Every namespace: default-deny `NetworkPolicy` + explicit minimal allows.
- No secret in a `ConfigMap`, manifest, or hand-set env var — Vault → External Secrets Operator
  only.
- All infra is declarative and GitOps-reconciled (ArgoCD); **no imperative `kubectl apply`** to a
  real environment.
- Terraform: remote state, locked, reviewed plans; no manual console changes.
- Autoscaling bounds set deliberately against DB `max_connections`.

---

## 5. Security Engineering Framework

Authority: `security-architecture.md` (supersedes briefer security statements elsewhere). Binding
engineering standards:

- **Authentication:** strong, phishing-resistant where possible; short-lived tokens; no long-lived
  static credentials in production (fail closed on placeholders); MFA for privileged roles.
- **Authorization:** deny-by-default; least privilege; centralized policy; every endpoint declares
  its authz; no "temporary" bypass without explicit, reviewed, labeled approval.
- **Encryption:** TLS in transit; envelope encryption at rest (ADR-0008); AES-256-GCM; no home-grown
  crypto — everything through `platform.crypto` (ADR-0009).
- **Key management:** provider-agnostic KMS; self-authenticating signature envelope; keys never in
  DB/app memory; rotation retains history; per ADR-0009.
- **Secrets:** Vault + ESO only; zero secrets in repo/config/logs; `.env.example` placeholders.
- **Audit logs:** append-only, attributable, tamper-evident (hash-chained target), covering authn,
  authz decisions, evidence access, lifecycle, and admin actions.
- **Evidence integrity:** see §6.
- **Supply-chain security & SBOM:** pinned dependencies; **SBOM (CycloneDX/SPDX) generated per
  build**; images signed (cosign) and admission-verified; provenance/attestation retained; only
  approved bases.
- **Secure SDLC:** threat model for every Tier 0/1 change; SAST/DAST/dependency/container scans in
  CI (§11) as hard gates; security review board sign-off (§12).
- **Threat modeling:** STRIDE + domain abuse cases; documented in the ADR; residuals accepted in
  writing.
- **Incident response:** severity taxonomy, on-call, containment/eradication/recovery runbooks,
  evidence-preserving forensics of our own platform, mandatory post-incident review feeding the
  risk register.

---

## 6. Digital Forensics Engineering Standards

Authority: `canonical-evidence-model.md`. These standards are **court-admissibility requirements**,
not preferences. Anything failing one does not ship.

| Concern | Engineering standard |
|---|---|
| **Chain of custody** | Every custody event (acquire/transfer/access/process/export) is an immutable, attributable, timestamped record signed under the Evidence Root; unbroken and reconstructable end-to-end. |
| **Evidence acquisition** | Source, method, tool, and operator recorded; original hashed at acquisition; acquisition itself is a custody event. |
| **Evidence storage** | Immutable, WORM-backed object storage; content-addressed; encrypted at rest; no in-place mutation ever. |
| **Evidence verification** | Independent verification engine recomputes hashes and validates signatures/custody chain on demand and on a schedule; result is itself auditable. |
| **Evidence processing** | Processing produces **derived** artifacts linked to the immutable original; originals are never altered; derivation is recorded. |
| **Evidence sharing** | Sharing is an authorized, audited export with an integrity manifest (hashes + signatures) a receiver can independently verify. |
| **Court admissibility** | Every evidentiary claim is reproducible offline from stored artifacts + public keys; the platform can produce a defensible integrity report per item and per case. |
| **Hash verification** | Approved algorithms only (policy-governed, agile); hashes recorded at ingest and re-verified; algorithm recorded with the hash for long-horizon agility. |
| **Legal hold** | **Any deletion/purge/retention path checks `legal_hold` first and refuses if held**; holds are audited; expiry does not override an active hold. |
| **WORM storage** | Retention/immutability enforced at the storage layer, not just the app; verified, not assumed. |
| **Digital signatures** | Self-authenticating envelope binding all metadata + required-algorithm set (downgrade-proof); providers sign raw bytes; per ADR-0009. |
| **Timestamping** | Authoritative time via external RFC-3161 anchoring (ADR-0003); application `created_at` is advisory only and must never be presented as trusted time. |
| **Verification engine** | A first-class, independently runnable subsystem that re-validates hashes, signatures, custody continuity, and anchoring; its output is admissible evidence of integrity. |

---

## 7. AI Governance Framework

AI in SentinelAI **assists investigation; it never adjudicates**. Standards:

- **Explainability:** every AI output carries its inputs, the evidence it cites, and a
  human-readable rationale; unexplained outputs are not surfaced as findings.
- **Hallucination mitigation:** ground outputs in retrieved, cited evidence; unsupported
  assertions are suppressed or flagged; no fabricated citations, entities, or facts.
- **Human-in-the-loop:** any AI-proposed finding/relationship/disposition requires explicit human
  confirmation before it changes case state (visible in the UI, per §8 and PRD FR-7.3).
- **Confidence scoring:** every inference carries a calibrated confidence; thresholds govern
  surfacing; low-confidence outputs are labeled, never hidden as fact.
- **Model & prompt versioning:** models and prompts are versioned, pinned, and recorded with every
  output so a result is reproducible and attributable to an exact configuration.
- **Prompt governance:** system/tool prompts are reviewed artifacts under change control; no
  untrusted content is ever treated as instructions (prompt-injection defense is mandatory).
- **Auditability & data lineage:** every AI action is logged with model/prompt version, inputs,
  evidence IDs, confidence, and the human decision; full lineage from source evidence to conclusion
  is reconstructable.
- **Responsible AI:** bias/fairness review for anything affecting persons; documented limitations;
  no autonomous action against an individual.
- **AI security:** injection, data-exfiltration, and model-abuse threats are in the threat model;
  AI components respect the same authz and audit boundaries as any other subsystem.

---

## 8. Frontend Engineering Standards

Authority: `frontend-architecture.md`. Standards:

- **Architecture:** React + TanStack Query; feature folders mirroring backend modules; strict
  four-way state split (server/UI/auth/URL).
- **Design system & component library:** a single tokenized design system (`packages/ui-components`);
  no ad-hoc styling; components documented with contracts and usage.
- **Accessibility:** WCAG 2.1 AA minimum — keyboard-navigable, screen-reader-labeled, sufficient
  contrast (verified in both themes); accessibility is a review-blocking criterion.
- **Responsiveness:** relative units, flex/grid, no horizontal body scroll; wide artifacts scroll
  in their own container.
- **Routing & state:** URL is the source of truth for shareable view state; server state via query
  keys; no derived server data duplicated into UI state.
- **Visualization standards:** graph (entity/relationship), timeline, and dashboard visualizations
  follow shared interaction and legend conventions; every visual element traceable to its evidence.
- **Investigation Workspace UX:** evidence-centric, low-friction, keyboard-first; destructive/
  consequential actions confirmed; AI suggestions clearly marked as pending human confirmation.
- **Dark theme:** first-class (investigator default); both themes must meet contrast and be tested.
- **Evidence viewer:** renders originals faithfully and read-only; shows integrity status (hash/
  signature/custody) inline; never mutates the source.
- **Graph & timeline:** performant at case scale; deterministic layouts where reproducibility
  matters; export-to-report supported.
- **Human-in-the-loop invariant:** review-status mutations are never optimistic.

---

## 9. API Governance

Authority: `api-design.md`. Standards:

- **REST:** resource-oriented URLs; standard methods; the shared response/error envelope; no verbs
  in paths.
- **Versioning:** explicit, backward-compatible within a major; breaking changes ⇒ new version +
  deprecation window + migration notes; no silent contract changes.
- **Pagination/filtering:** consistent, documented pagination; filtering via a defined query
  grammar; bounded page sizes.
- **Idempotency:** all unsafe, retryable operations accept an idempotency key; replays are safe.
- **Error contracts:** every error uses the standard envelope with a stable machine code; no leaking
  internals/stack traces.
- **OpenAPI:** the spec is generated and is the contract; the SDK (`packages/sdk`) derives from it;
  CI fails on spec/impl drift.
- **Authentication & rate limiting:** every endpoint declares authn/authz; rate limits and quotas
  defined per client class; abuse fails closed.
- **Future gRPC:** internal high-throughput/streaming paths may adopt gRPC as a *complementary*
  transport (ADR-gated); the REST contract remains the external interface unless an ADR changes it.

---

## 10. Data Engineering Standards

Authority: `database-design.md`. Standards:

- **PostgreSQL:** schema-per-module ownership; **no cross-schema FK** (app-validated UUID refs);
  migrations reviewed, DAG-ordered, reversible; parameterized queries only.
- **Object storage:** content-addressed, WORM for evidence, encrypted at rest, lifecycle-governed.
- **Search:** a dedicated search index derived from source-of-truth data via events; never the
  system of record.
- **Graph:** entity/relationship graph derived from the canonical model; consistency maintained via
  events, not cross-writes.
- **Analytics:** served from read models/derived stores; never by heavy queries against
  transactional evidence tables.
- **Data retention & archival:** retention policies per data class; archival to colder tiers is
  audited and **legal-hold-aware** (hold overrides expiry).
- **Multi-tenancy:** deployment-profile-appropriate isolation (state/central/enterprise/SaaS per
  `deployment-architecture.md`); isolation model is an ADR decision, verified not assumed.
- **Backup & DR:** automated, encrypted, tested backups; documented RPO/RTO; restore **drilled in
  staging** before trusted; DR failover exercised; post-recovery evidence re-verification.

---

## 11. CI/CD Standards

The pipeline is the enforcement mechanism for this manual. **A change that would fail CI is not
finished.** Stages (each a hard gate unless marked advisory):

1. **Lint & format** — ruff (Python), eslint/prettier (TS), hadolint (Docker), tflint (Terraform).
2. **Type check** — mypy (strict on Tier 0/1), `tsc --noEmit`.
3. **Architecture validation** — import-linter boundary rules; module-DAG check; no deep imports.
4. **ADR compliance** — code implementing a decision must reference an Accepted ADR; new endpoint/
   event/table absent from its authoritative doc fails the build.
5. **Unit tests + coverage** — floors per tier (§4); invariant tests required for Tier 0.
6. **Integration/contract tests** — cross-module contracts, event flows, OpenAPI ↔ impl parity.
7. **Security scans** — SAST, secret scanning, DAST (advisory→gating as it matures).
8. **Dependency scan** — known-CVE and license policy; **SBOM generated and stored**.
9. **Container scan** — image CVE scan; base-image allowlist; **cosign sign + verify**.
10. **Performance benchmarks** — regression vs. baseline; NFR budgets enforced for Tier 0/1.
11. **Deployment gates** — GitOps only; migrations as ArgoCD `PreSync` in module-DAG order; signed
    images only; canary/health verification; validated rollback exercised in staging.

No stage may be skipped for a "quick fix." Bypassing a gate requires a labeled, time-boxed, board-
approved exception recorded in the risk register.

---

## 12. Review Boards

Boards are **roles and responsibilities**, not necessarily distinct people at current team scale;
one engineer may chair several, but each sign-off is recorded under its board so accountability and
future scaling are explicit. A board **owns its Quality Gate(s) (§3)** and the corresponding SDLC
gate (§1).

| Board | Owns | Blocks | Sign-off required for |
|---|---|---|---|
| **Architecture Review Board (ARB)** | Clean Arch, DDD, SOLID, CQRS/ES suitability, Maintainability, Cloud readiness | Structural defects, boundary violations | Every ADR; every Tier 0/1 change |
| **Security Review Board (SRB)** | Security, Privacy, Compliance gates; threat models | Insecure/injection-prone/privacy-violating designs | Anything touching authn/z, crypto, secrets, PII, evidence integrity |
| **Digital Forensics Review Board (DFRB)** | Forensic integrity gate | Anything weakening custody/immutability/admissibility | Every change to `forensics`/`case-management`/evidence paths |
| **AI Review Board (AIRB)** | AI governance gate | Unexplainable/ungrounded/non-HITL AI | Every AI capability or model/prompt change |
| **Performance Review Board (PRB)** | Scalability + DR gates | NFR/latency/DR failures | Tier 0/1 perf-sensitive changes; DR designs |
| **Production Readiness Board (PRR)** | Operability, observability, rollback, runbooks | Un-operable or un-recoverable releases | Every production deployment |

A board may return `Approved`, `Approved with required fixes` (re-review after fixes), or
`Rejected` (redesign). Tier 0 verdicts require an **independent adversarial audit** whose reviewer
did not author the design (precedent: ADR-0009).

---

## 13. Definition of Done

A feature/ADR/subsystem is **Done** only when **all** hold (no partial credit; "Done" is a claim
of fact, not optimism):

1. Requirement traced to an FR/NFR/SR and satisfies its acceptance criteria.
2. An Accepted ADR governs any architecturally significant decision it introduced.
3. All applicable Quality Gates (§3) pass; residuals accepted in writing.
4. Threat model complete (Tier 0/1); no open Critical/High security finding.
5. Code conforms to §4 and `backend-implementation-guide.md`; no TODO/stub/`pass`-as-done.
6. Tests: unit + integration + (perf for Tier 0/1) **written and green**; coverage floor met;
   every security/evidentiary invariant explicitly tested.
7. Boundary/ADR-compliance/lint/type checks green in CI.
8. Code review approved (2 reviewers for Tier 0/evidence/crypto).
9. For evidentiary paths: custody, immutability, hash-verification, legal-hold, and admissibility
   demonstrated by test.
10. For AI paths: explainability, HITL, confidence, and lineage demonstrated.
11. Documentation updated in the same change (ADR, domain doc, module README, runbook); doc-sync
    check green.
12. Observability: metrics/logs/traces emitted; dashboards/alerts where warranted.
13. Operability: runbook + **validated rollback** + DR impact assessed; PRR passed.
14. SBOM produced; image signed; dependencies within policy.

---

## 14. Long-Term Technology Roadmap (2026–2031)

Strategic evolution; specifics are ADR-gated as they arrive. Aligns with `roadmap.md` (phase exit
criteria) and `engineering-roadmap.md` (task-level plan). Absolute dates from a 2026 baseline.

| Horizon | Platform maturity | Architecture / microservices | AI evolution | Cloud & HA | Multi-agency / i18n | Compliance |
|---|---|---|---|---|---|---|
| **2026 — Foundation** | Modular monolith; core evidentiary + KMS foundations (ADR-0009→0003→0008→0007→0010) | Single deployable, hard module boundaries; extraction-ready | Assisted correlation, HITL findings, confidence scoring | Single-cluster HA; GitOps; drilled backup/restore | Single-agency; i18n-ready foundations (externalized strings) | Internal control baseline; audit-log + evidence integrity provable |
| **2027 — Hardening** | Full five-domain MVP; verification engine GA | Still monolith; first extraction *candidate* identified only if a real bottleneck appears | Grounded RAG over evidence; prompt governance; model versioning | Multi-AZ HA; RPO/RTO targets met and exercised | Second agency profile (state/central); first non-English locale | Pursue first external certification track (e.g. ISO 27001-class) |
| **2028 — Scale** | High-volume ingestion & analytics; graph at case scale | Extract **only** proven-hotspot modules to services (ADR per extraction); event backbone matured | Cross-domain hypothesis assistance; calibrated confidence; bias review | Multi-cluster/region; active-passive DR failover drilled | Multi-tenant single-instance; several locales incl. RTL | Add sector certification (e.g. SOC 2-class); chain-of-custody legal review |
| **2029 — Federation** | Cross-agency intelligence sharing with integrity manifests | Selective services + strong contracts; gRPC internal where warranted (ADR-gated) | Agentic investigation workflows with mandatory HITL checkpoints | Active-active where profile demands; regional data residency | International LE deployment; broad i18n/l10n | Court-admissibility certification per jurisdiction; PQC migration planning |
| **2030–2031 — Longevity** | Sustained 15–20-yr operability; crypto-agility exercised | Steady-state modular-services topology; no premature decomposition | Continuous evaluation; model/prompt lifecycle; responsible-AI attestations | Optional multi-tenant SaaS profile; DR maturity | Global multi-agency; full localization program | Maintain certifications; **execute hybrid PQC dual-sign migration** (ADR-0009 ceremony) |

**Roadmap governance:** microservice extraction is a **Phase-5 / bottleneck-triggered** decision
requiring an ADR and ARB approval — never anticipated by default. Every horizon transition passes
the PRR and preserves evidentiary integrity and backward verifiability without exception.

---

## Amending this constitution

This manual is itself governed. Changes require an ADR, ARB approval, and — for anything touching
security, forensic integrity, or AI governance — the relevant board's sign-off. Amendments are
versioned by supersession, never silent edits to a standard. The **Governing Principles (§0) are
foundational**: weakening one requires a documented, board-approved, mission-level justification.
