# SentinelAI — Independent Whole-System Architecture Validation

**Review type:** Adversarial, reject-first, whole-platform integration review (not per-ADR).
**Reviewer stance:** Independent Principal Architect (external). Objective: reject if possible.
**Date:** 2026-07-27. **Corpus:** `docs/*` (18 docs, 14 ADRs) — design only; no code exists yet.
**Governance context:** this report is the SDLC §1 stage-12 "Independent Audit" artifact for the
platform as a whole, evaluated against `engineering-governance.md` §3 Quality Gates.

---

## 0. Method & the scope-reality finding (read this first)

The validation prompt enumerates ~55 "subsystems." The **designed** architecture is: 8 domain
modules + a `platform` layer + 14 ADRs. Roughly one-third of the named subsystems are **not
designed** — they are ambitions in `vision.md`/`prd.md` or artifact *categories* in the canonical
evidence model, not architected components. An honest whole-system verdict must grade the **real
design** and flag the **scope-to-design gap** as its own risk, rather than fabricate reviews of
components that do not exist.

| Named subsystem | Design status in the corpus |
|---|---|
| Auth, Authorization, Identity, Sessions, RBAC/ABAC | **Designed** — ADR-0010 (+ `case_members`, opaque server-side sessions, MFA/SSO) |
| KMS / Key Management | **Designed** — ADR-0009 (revised, architecture-approved) |
| Evidence Integrity, Chain of Custody | **Designed** — ADR-0003 (authenticated, anchored, crypto-agile ledgers), CEM |
| Append-only / DB protections | **Designed** — ADR-0004 (role separation, revocation, triggers, ledger-derived legal hold) |
| Object Storage / WORM | **Designed** — ADR-0008 (quarantine→scan→promote, Object-Lock, server-side hashing) |
| Event Bus / Outbox / Inbox / Dispatcher | **Designed** — event-driven-arch.md + ADR-0006/0007 |
| Transaction Boundaries | **Designed** — ADR-0005 (UoW at entrypoint) |
| CQRS / Graph read models | **Designed (Phase 2)** — ADR-0013 (projections; graph DB deferred, benchmark-gated) |
| Multi-tenancy | **Partially** — ADR-0014 (physical isolation default; SaaS profile pending product decision) |
| API idempotency / REST / versioning | **Designed** — ADR-0012 + api-design.md |
| Case Management, Investigation (AI correlation), Notification | **Designed** — modules + system-design |
| Ingestion, OSINT, Threat-Intel, Forensics, Social-Media | **Designed as modules** (connectors/parsers themselves are placeholders) |
| Search | **Named, not designed** (derived index implied by CQRS, no ADR) |
| Graph Intelligence | **Read-model designed; graph engine deferred** |
| Entity Resolution | **Named** (DB/roadmap mentions) — **not designed** |
| Workflow Engine | **Not designed** as an engine — case/finding are hardcoded aggregate state machines (ADR-0011) |
| Scheduler | **Not designed** — no cron/scheduling subsystem despite multiple periodic obligations |
| Blockchain Intelligence | **Evidence *category* only** — no analysis subsystem |
| Mobile / Computer / Cloud / Drone Forensics | **Collapsed into one generic `forensics` module** — no per-discipline design |
| Video Analytics, Face Recognition | **Not designed at all** (0 references) |
| Plugin Architecture / SDK | **Named as future (Phase 4+)** — not architected as a trust-bounded SPI |
| API Gateway | **Target-topology only** (Phase 4–5) |
| Zero Trust | **Partially** — intra-Phase-1 (signed events, opaque sessions); post-extraction mTLS/mesh undesigned |
| Observability / Monitoring / Logging | **Designed** — system-design §12 (audit-vs-telemetry separation) |
| HA / DR / Backup / Deployment / K8s / Network / Supply-chain | **Designed** — deployment-architecture.md (CloudNativePG, cosign, ArgoCD, NetworkPolicies, SBOM) |

**Consequence:** the platform is, today, an exceptionally well-architected **evidence-integrity +
cross-domain-correlation backbone** with a deliberate extraction path. It is **not yet** the full
"intelligence suite" the vision advertises. That gap is a *scope/capability* risk, not a
*structural* defect — the backbone is extension-shaped (connectors and artifact-types feed the
canonical model), so the missing capabilities extend it rather than force a rewrite.

---

## 1. Per-subsystem verdict (designed subsystems)

Each subsystem scored against the 20 review questions, collapsed to a verdict + the dimensions
where it is weak. "✓" = no material violation found. Only exceptions are listed.

| Subsystem | Correct? | Clean/DDD/EDA | SecByDesign / ZeroTrust | Evidence principles | National scale | Failure / Insider / Ransomware / DR | Debt / Coupling / Migration / Lock-in | Cloud-neutral / On-prem / Sovereign / Classified |
|---|---|---|---|---|---|---|---|---|
| **Auth/Identity/Sessions (0010)** | ✓ | ✓ | ✓ opaque revocable sessions, RBAC+ABAC | n/a | ✓ (stateless http, Redis session) | insider ✓; DR of session store ok | low debt; IdP adapter | ✓ offline IdP required (air-gap) |
| **KMS (0009)** | ✓ | ✓ crypto is platform-layer, 0 module imports | ✓ fail-closed, self-auth envelope | ✓ signs custody/anchors | ✓ providers scale | key-loss = **highest-consequence SPOF**; escrow designed, not operated | provider-agnostic (no lock-in) | ✓ Vault/HSM on-prem, classified-ok |
| **Evidence integrity (0003)** | ✓ | ✓ | ✓ signature-based, not bare hash | ✓ anchored+crypto-agile | anchoring cadence unmodeled | insider ✓ (key outside DB role); ransomware ✓ (anchor) | **all Proposed/unexecuted** | ✓ air-gap anchoring needs offline trusted time |
| **Append-only DB (0004)** | ✓ | ✓ | ✓ role separation | ✓ WORM-at-DB | ✓ | superuser-drops-triggers residual (covered by 0003) | low | ✓ |
| **Object storage / WORM (0008)** | ✓ | ✓ port-abstracted | ✓ quarantine, server hash authoritative | ✓ Object-Lock compliance | **scan-before-promote throughput** at media scale | ransomware ✓ (Object-Lock); DR: object/DB RPO divergence | S3-API (no lock-in) | ✓ MinIO on-prem |
| **Event bus / outbox / inbox (0006/0007)** | ✓ | ✓ at-least-once + idempotent | ✓ signed envelopes (zero-trust between modules) | ✓ event authenticity | ✓ competing consumers, per-aggregate order | DLQ **"defined-but-unspecified"** | transport-swap seam clean | ✓ in-proc→Redpanda |
| **Transaction boundaries (0005)** | ✓ | ✓ UoW at entrypoint | ✓ | ✓ atomic custody+outbox | ✓ | ✓ | **cross-module sync WRITE would break extraction** | ✓ |
| **CQRS / graph read models (0013)** | ✓ | ✓ disposable projections | ✓ | ⚠ **projections must never feed court export** | ✓ read-scaling | rebuildable from log | graph DB deferred (evidence-gated) | ✓ |
| **Multi-tenancy (0014)** | Partial | ✓ schema-per-tenant / physical | ✓ per-tenant keys/buckets | ✓ tenant in audit | ✓ | isolation strong | **profiles-in-scope decision pending** | ✓ sovereign/air-gap = physical isolation |
| **API / idempotency (0012, api-design)** | ✓ | ✓ envelope, versioned | ✓ authn per endpoint | ✓ | rate-limit policy absent | replay-safe | OpenAPI-driven SDK | ✓ |
| **Case Management** | ✓ | ✓ system of record | ✓ | ✓ custody log, court report | low-throughput/high-integrity | ✓ | **2nd cross-domain hub (understated)** | ✓ |
| **Investigation (AI correlation)** | ✓ | ✓ sole cross-domain reader | ✓ HITL non-optimistic | ✓ findings cite evidence | **AI compute at national scale unmodeled** | graceful degrade ✓ | **AI-provider lock-in vs air-gap** | ⚠ needs self-hosted models for classified |
| **Ingestion + collection modules** | ✓ | ✓ normalize→canonical | ✓ | ✓ integrity metadata | **ingest spikes; per-source isolation** | ✓ bulkhead | connectors = undesigned SPI | ✓ |
| **Notification** | ✓ | ✓ consumes events | ✓ own audit trail | n/a | ✓ | delivery guarantees under-specified | low | channel-availability-tolerant |
| **Observability / Audit-telemetry split** | ✓ | ✓ separate systems | ✓ | ✓ audit ≠ telemetry | ✓ RED/USE | ✓ | W3C trace-context now | ✓ |
| **Deployment / K8s / Supply-chain** | ✓ | ✓ GitOps | ✓ default-deny netpol, cosign, SBOM | n/a | ✓ HPA vs max_connections | DR: **no quantified RPO/RTO**; **backup immutability unstated** | Harbor/ArgoCD (portable) | ✓ air-gap zero-egress |

---

## 2. Interaction, cycle & consistency analysis

**Module event graph (source of truth = system-design §5 + §6 catalog):**
collectors → `ingestion` → `case-management` → `investigation` → `notification`, with
`investigation` also fanning in directly from every collector, and `case-management` ↔
`investigation` bidirectional.

**Cyclic dependencies.**
- **CYCLE (event-level): `case-management` ↔ `investigation`.** case-management emits
  `evidence.linked_to_case` and `investigation.finding_reviewed` *to* investigation; investigation
  emits `investigation.correlation_generated` *to* case-management. This is not a compile-time
  import cycle (events decouple it, which is legitimate for EDA), but it is the platform's **tightest
  coupling** and a genuine **feedback-loop hazard**: review → case update → re-correlation → new
  finding → review… **No explicit convergence/loop-break rule is documented.** *Finding #4/#14.*
- No other true cycles. The prompt's hypothetical `notification → audit → evidence` chain does **not**
  exist in the design (notification writes only its own delivery records; audit is per-module and not
  written by notification).

**Hidden coupling.**
- **`case-management` is a second cross-domain hub** (it links evidence from all five collectors and
  is read by investigation), yet the docs assert investigation is "the only cross-domain module."
  The centrality of case-management is understated; it is the real availability-critical core.
  *Finding #11(hub).*
- **Dual evidence ownership.** Each collector keeps native records *and* normalizes into the
  canonical evidence model surfaced via ingestion/case-management. **Which record is authoritative
  for chain-of-custody is not unambiguously pinned.** *Finding #3.*

**Bounded-context violations.** None structural. The one sanctioned cross-context reader
(investigation) is correctly the only one. Watch item: request/response public-interface calls must
remain **reads only**; any cross-module synchronous **write** re-introduces a distributed
transaction the moment a module is extracted. *Finding #5.*

**Weak / dangerous eventual consistency.**
- **Court export / evidentiary reads must be sourced from the write-model source of truth, never
  from CQRS projections or the search index.** ADR-0013 scopes projections to "analytical/review"
  reads — correct intent — but this is **not yet an enforced invariant**, and it is the single
  place where eventual consistency would silently produce a non-defensible legal artifact.
  *Finding #1 (top-ranked).*
- **Review-queue projection staleness + concurrent analysts** → double-disposition race. The
  non-optimistic HITL rule reduces the UI surface but does not by itself provide write-side
  optimistic-concurrency versioning. *Finding #15.*
- **Object-store vs DB RPO divergence** on failover → evidence *metadata* (Postgres) and *payload*
  (object store) can land at different recovery points. *Finding #41.*

**Data duplication.** All intentional and rebuildable (CQRS projections, search index, graph read
model, RAG embeddings) — acceptable *provided* none becomes a second source of truth. Embedding-of-
evidence in the vector store is an unspecified **PII/egress surface**. *Finding #37.*

**Transaction correctness.** ADR-0005 (UoW at entrypoint) + outbox-in-same-transaction is correct;
cross-module consistency is event-based (no distributed transactions) — the right call. The only
correctness risk is the cross-module-synchronous-write watch item (#5).

**Evidence-integrity break points.** (a) projections feeding court export (#1); (b) key loss/escrow
(#22); (c) legal-hold split across DB-ledger (0004) and object legal-hold (0008) must be atomically
consistent (#21); (d) trusted-time dependency on not-yet-implemented anchoring (#10);
(e) imported cross-agency evidence provenance/re-anchoring (#40).

---

## 3. Cross-cutting architecture reviews

- **Database:** schema-per-module, no cross-schema FK (app-validated UUIDs), append-only
  evidentiary tables, three-role separation — **strong**. Phase-1 single Postgres instance is the
  availability SPOF until HA (acknowledged, deferred). *Finding #44.*
- **Storage:** quarantine→scan→promote→WORM, server-authoritative hashing — **strong**; scanner
  throughput and object/DB RPO reconciliation are the open risks (#24, #41).
- **Event:** outbox/inbox, at-least-once, signed envelopes, out-of-process order-preserving
  dispatcher — **strong**; DLQ unspecified (#12), per-domain queue priorities absent (#36).
- **Deployment/K8s/CI-CD/Supply-chain:** GitOps, cosign-signed images, default-deny NetworkPolicies,
  SBOM, PreSync migrations in DAG order — **strong and mature for the stage**. Quantified RPO/RTO and
  immutable/air-gapped backups are the gaps (#6, #7).
- **Security:** defense-in-depth is genuinely excellent (KMS + append-only + signed events + WORM +
  opaque sessions + ABAC). Zero-Trust is complete *within* Phase 1 but **service-to-service authN/Z
  after extraction (mTLS/SPIFFE/mesh) is undesigned** (#13). Nothing is executed; most ADRs are
  Proposed (#2).
- **Observability:** audit-vs-telemetry separation is a mature, correct decision. Audit-log tamper-
  evidence must be unified with ADR-0003's chaining (#23).
- **AI:** HITL-correct, explainability/lineage required by governance, provider-agnostic port — good
  bones. But: per-inference lineage not yet in the data model (#38), prompt-injection from
  untrusted OSINT/social not threat-modeled (#39), and provider strategy unresolved for air-gap
  (#8). The **advertised AI capabilities (face recognition, video analytics, entity resolution,
  blockchain intel) are undesigned** (#17, #26–#29).
- **Plugin/SDK:** connectors and forensic parsers are the platform's untrusted-code ingress and are
  **not yet architected as a sandboxed, trust-bounded SPI** — a supply-chain surface (#30, #31).

---

## 4. Scores (independent, harsh)

| # | Dimension | Score | Basis |
|---|---|---|---|
| 1 | **Architecture** | **90/100** | Excellent bounded contexts + extraction-ready + ADR discipline; −: case↔investigation cycle, dual-ownership ambiguity, understated case-mgmt centrality |
| 2 | **Security** | **88/100** | Outstanding defense-in-depth; −: all foundational ADRs Proposed/unexecuted, post-extraction s2s authZ undesigned, backup immutability unstated |
| 3 | **Reliability** | **82/100** | Strong FT principles, bulkheading, idempotency; −: no quantified RPO/RTO, unified DR runbook missing, DLQ unspecified |
| 4 | **Scalability** | **85/100** | Clear phase strategy, stateless http, extraction recipe, CQRS; −: national-scale capacity/AI-compute/ingest-spike unmodeled |
| 5 | **Maintainability** | **92/100** | Best dimension: enforced boundaries, governance manual, doc-code-sync, ADR traceability |
| 6 | **Intelligence Capability** | **70/100** | Correlation/AI backbone is well-designed & HITL-correct, but the headline intelligence suite (face/video/entity-resolution/blockchain) is undesigned — grading design *coverage* of the claimed capability |
| 7 | **Digital Forensics Readiness** | **90/100** | Authenticated anchored crypto-agile ledgers, WORM, ledger-derived legal hold, verification engine, court export; −: unratified FIPS/algorithm, trusted-time & projections-in-export invariants not yet enforced |
| 8 | **Production Readiness** | **55/100** | This is design; near-zero implemented/executed; ADRs mostly Proposed. Appropriately low for scaffold phase — reflects distance-to-production, not quality |
| 9 | **Technical Debt (100=none)** | **85/100** | Remarkably low *rot*-debt for the stage; existing debt is scope-debt (undesigned subsystems) + unratified ADRs, not accumulated shortcuts |
| 10 | **Future Risk (100=lowest)** | **70/100** | Main risks: undesigned high-compute AI subsystems, AI-provider vs air-gap parity, unmodeled national scale, unratified/unexecuted foundations, no scheduler |

---

## 5. TOP 50 architectural weaknesses (ranked by severity)

Cost scale: **S** ≤ days, **M** ≈ 1–3 wks, **L** ≈ 1–2 mo, **XL** ≥ quarter/systemic. "Now" =
resolve in design; "Later" = after implementation.

### Critical / architecture-defining (resolve before or in the first implementation wave)
| # | Weakness | Now | Later |
|---|---|---|---|
| 1 | Court-export / evidentiary reads not yet a **hard invariant** to read write-model source of truth (never CQRS/search projections) | S | **XL** |
| 2 | Entire evidence-integrity chain (ADR-0003/0004/0007/0008/0009) still **Proposed & unexecuted** | M | **XL** |
| 3 | **Dual source-of-truth ambiguity** for an evidence item (collector vs ingestion vs case-mgmt) for chain-of-custody | S | L |
| 4 | `case-management ↔ investigation` **event cycle** has no documented convergence/loop-break rule | S | L |
| 5 | No rule forbidding **cross-module synchronous writes** → Phase-5 extraction inherits a distributed-transaction break | S | **XL** |
| 6 | **No quantified RPO/RTO** and no unified DR runbook spanning DB + object store + key material + anchors | M | L |
| 7 | **Backup immutability / air-gap not specified** → ransomware can target backups despite WORM primary | S | M |
| 8 | **AI-provider strategy unresolved**; air-gap/classified need self-hosted models → capability-parity risk across profiles | M | L |
| 9 | **National-scale capacity & cost model absent** (ingest spikes, AI compute, media-evidence storage growth) | M | L |
| 10 | **Trusted timestamp** depends on external RFC-3161 anchoring not yet implemented; no court-grade time until then | M | L |

### High
| # | Weakness | Now | Later |
|---|---|---|---|
| 11 | **No scheduler subsystem** for anchoring cadence, scheduled verification, retention/legal-hold sweeps, key renewal, idempotency cleanup | S | M |
| 12 | **Dead-letter / poison-message path "defined-but-unspecified"** — forensic jobs must never be silently dropped | S | M |
| 13 | **Post-extraction service-to-service authN/Z (mTLS/SPIFFE/mesh) undesigned** — Zero Trust incomplete beyond Phase 1 | M | L |
| 14 | No explicit **re-correlation loop guard** in investigation on `finding_reviewed` | S | M |
| 15 | **Review-queue projection staleness + concurrent analysts** → double-disposition; needs write-side optimistic concurrency | S | M |
| 16 | **Multi-tenancy profiles-in-scope** product decision pending — blocks storage/key/backup design for SaaS | S | L |
| 17 | **Entity resolution undesigned** — correlation quality depends on cross-domain identity dedup | M | L |
| 18 | **Search architecture undesigned** (access-control-filtered, evidentiary relevance at national scale) | M | L |
| 19 | **Graph datastore decision deferred** — national-scale traversal latency is a live risk; benchmark gate must be real | M | L |
| 20 | **PII/privacy data-flow map absent** (field classification, per-class retention, subject rights) | M | L |
| 21 | **Legal hold split** across DB-ledger (0004) and object legal-hold (0008) — cross-subsystem atomic consistency unspecified | S | L |
| 22 | **Key DR/escrow (M-of-N) not operationalized** — key loss voids evidence; highest-consequence SPOF | M | L |
| 23 | **Audit-log tamper-evidence** not yet unified with ADR-0003 hash-chaining across modules | M | L |
| 24 | **Scan-before-promote throughput** blocks evidence availability at national media volumes | M | L |
| 25 | **Compliance certification targets unpinned** (CJIS/ISO 27001/SOC 2/national) → controls can't be designed to a target | S | L |

### Medium
| # | Weakness | Now | Later |
|---|---|---|---|
| 26 | **Face recognition** — named capability, undesigned; privacy/bias/accuracy/admissibility; needs ADR + AIRB | M | L |
| 27 | **Video analytics** — undesigned; streaming-evidence storage/compute + derived-artifact custody | M | L |
| 28 | **Blockchain intelligence** — evidence category only; tracing/clustering subsystem undesigned | M | L |
| 29 | **Multi-discipline forensics collapsed** into one generic module — mobile/computer/cloud/drone differ materially | M | L |
| 30 | **Connector/parser plugin SPI** not architected as sandboxed, trust-bounded (untrusted-code ingress) | M | L |
| 31 | **SDK + external-integration contract** (RMS/SIEM) deferred, auth model undesigned | S | M |
| 32 | **Notification delivery guarantees** (at-least-once, dedupe, alert audit) under-specified | S | M |
| 33 | **Reporting/court-export format standard** (PDF/A + integrity manifest) unspecified | S | M |
| 34 | **Rate-limiting/quota + DDoS posture** per client-class named but not concrete | S | M |
| 35 | **Idempotency scoped per principal** — connector-driven / cross-principal retries need review | S | M |
| 36 | **Single job queue** — national scale needs per-domain queues/priorities (parse vs poll vs AI) to avoid head-of-line blocking | S | M |
| 37 | **RAG embeddings of evidence** in vector store — PII/egress governance unspecified | S | M |
| 38 | **Per-inference AI lineage** (inputs+evidence+model+prompt+confidence) not in the data model | M | L |
| 39 | **Prompt-injection from untrusted OSINT/social** not threat-modeled | S | M |
| 40 | **Cross-agency imported-evidence provenance/re-anchoring** trust model unspecified | M | L |

### Low / hygiene
| # | Weakness | Now | Later |
|---|---|---|---|
| 41 | Object-store vs DB **RPO divergence** on failover → metadata/payload split | S | M |
| 42 | **PII-in-logs redaction** policy not concretely enforced despite trace-context choice | S | S |
| 43 | **Redis triple-duty** (cache + Phase-1 queue + sessions) — failure blast radius under-analyzed | S | M |
| 44 | **Single Postgres = availability SPOF** until HA (acknowledged, deferred) | S | M |
| 45 | **Offline trusted-time source** for air-gapped RFC-3161 anchoring unspecified | S | M |
| 46 | **Per-profile config/feature-flag governance** absent → deployment-profile drift risk | S | M |
| 47 | **Test-corpus data-handling standard** (no real PII in fixtures) absent | S | S |
| 48 | **i18n/l10n foundations** (RTL, locale) named, no concrete plan — costly to retrofit | S | L |
| 49 | **Accessibility (WCAG 2.1 AA)** asserted but not CI-gated with real tooling | S | M |
| 50 | **FinOps/cost observability** (media-evidence storage growth dominates) not tracked | S | M |

---

## 6. Exponential-rework (STOP) determination

**No weakness triggers exponential future rework. The program does NOT stop.** The three
properties that would cause exponential rework if wrong — bounded-context boundaries, the
evidence-integrity/custody model, and event-based (not distributed-transaction) inter-module
consistency — are **correct**. The undesigned capabilities (search, entity resolution, face/video/
blockchain intel, multi-discipline forensics, plugins/SDK) attach to a canonical evidence model and
event backbone that were explicitly designed to be *extended*, so they are **additive (linear),
not multiplicative (exponential)**.

The two findings that *could* become exponential **if ignored** — #1 (projections in the court-
export path) and #5 (cross-module synchronous writes) — are both **cheap to fix now (S)** and are
converted from XL-later to S-now simply by writing them down as enforced invariants **before**
implementation. That is precisely why this gate exists.

---

## 7. Certification

**VERDICT: APPROVED TO ENTER THE IMPLEMENTATION PHASE — conditionally, with no stop-the-program
finding.**

The architecture is structurally sound, forensically serious, security-mature, and built for a
20-year horizon. It is certified to proceed into implementation **under the following binding
conditions**, which must be adopted into the governance/ADR record *before or during* the first
implementation wave:

1. **Enforce invariant #1** — evidentiary/court-export reads come only from the write-model source
   of truth; projections/search are never legal artifacts. Add to `engineering-governance.md` §6
   and the CQRS ADR.
2. **Enforce invariant #5** — cross-module public-interface calls are read-only; all cross-module
   state change is event-mediated. Add to the module-boundary rules.
3. **Pin the convergence rule for #4/#14** (idempotent, non-amplifying re-correlation).
4. **Resolve the two open product decisions gating design** — multi-tenancy profiles in scope
   (#16) and AI-provider/air-gap strategy (#8) — as ADRs.
5. **Schedule findings #1–#25** into the engineering roadmap with owners before the waves they
   affect; #6/#7/#10/#22 (DR, backup immutability, trusted time, key escrow) must be resolved
   before any real evidence is stored in production.

This certification covers the **architecture**. It explicitly does **not** certify the
implementation (Production Readiness 55/100 by design), and it does not lower the ADR-0009 FINAL
gate (execution validation still pending). Re-validation is required if any of the Critical findings
is resolved in a way that changes a bounded context, the consistency model, or the evidence chain.
