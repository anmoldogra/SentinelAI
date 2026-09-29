# 14. Multi-Tenancy / Multi-Agency Isolation

## Status

**Accepted (2026-09-29).** The product decision this ADR was blocked on has been made: **SentinelAI
supports exactly one tenancy model — one deployment per agency, physically isolated.** There is no
shared-infrastructure tier, and offering one requires a superseding ADR rather than an
implementation.

This rewrites the `Proposed (Phase 2)` draft in place, which is permitted because that draft was
never Accepted (ADR-0001: "ADRs are immutable *once accepted*"). The draft's conditional decisions —
schema-per-tenant for a future SaaS profile, and activating the reserved tenant context end-to-end —
are **not carried forward as commitments**; they are recorded below as the alternatives they always
were.

| Decision | State |
|---|---|
| §1 Physical/deployment isolation per agency is the supported model | **Decided** — and already true of every shipped deployment profile |
| §2 No shared-infrastructure tier; the "Future SaaS" profile is out of scope | **Decided** — a superseding ADR is the only route in |
| §3 The reserved `tenant_id` context stays reserved, permanently `None` | **Decided** — pinned by `tests/architecture/test_tenant_isolation.py` |
| §4 Cross-agency sharing stays explicit, audited and event-mediated | **Decided** — unchanged from the draft; nothing implements it yet |
| Row-level `tenant_id` + RLS | **Rejected** — see "Alternatives considered" |
| Schema-per-tenant | **Rejected for now** — the least-bad shared model if §2 is ever revisited |

**This ADR is satisfied by building nothing.** That is not a deferral. Isolation is delivered by the
deployment boundary, and the correct amount of application code for that is zero.

## Context

`security-architecture.md` §40 has carried a recommendation since the architecture phase — physical,
dedicated-deployment tenancy as the default, with shared-schema multi-tenancy as an explicitly
separate evaluation "never the default" — and flagged it (§51) as requiring a formal ADR "before any
Phase 4 multi-tenancy work begins". `engineering-roadmap.md`'s open-ADR register carried it as
**Open**, and its Phase 4 milestone (M4) asks for "an actual decision recorded (even if that decision
is 'not now, single-tenant remains the default') rather than sitting open past this phase".
`modernization-roadmap.md`'s Wave 4.2 row named the gate precisely: **"product decision on profiles
first."**

That decision has now been taken by the product owner. It resolves in the direction the document set
already pointed, which means the outcome is not a change of course but the removal of an open
question that was blocking a phase gate.

**What the platform already is.** `deployment-architecture.md` Part 22 defines four profiles. Three —
state/local police department, central/federal agency, single-tenant enterprise — are already
"Single-tenant, dedicated"; the central-agency row is "Single-tenant, dedicated, physically
isolated". The fourth is "Future SaaS (Phase 4+) ... **not the default**". So the decision below makes
the shipped reality the *supported* reality, rather than leaving three built profiles and one
speculative one competing for the same codebase.

**Why the customer base settles it.** The target buyers are police departments, federal and
intelligence agencies, and enterprise security teams handling evidence intended to be
court-admissible over a 15–20 year horizon (`engineering-governance.md`'s stated horizon). §40 puts
the commercial reality bluntly: logical isolation "asks government/intelligence customers to trust a
shared-infrastructure boundary many will not accept by policy". A control that the buyer's own policy
forbids them to rely on is not a cheaper control; it is an unsellable one.

## Decision

### §1 Physical/deployment isolation per agency is the supported tenancy model

Each agency receives its own deployment: **its own database cluster, its own KMS root key (ADR-0009),
its own object storage and buckets (ADR-0008), its own network zone.** No component is shared across
agencies — not the database, not the event bus, not the key hierarchy, not the audit ledger.

Cross-tenant data access is therefore not prevented by a predicate that has to be correct on every
query. It is prevented because the other agency's rows are in a different Postgres cluster, reachable
only from a different network zone, encrypted under a key this deployment has never held.

This satisfies the standing constraint that isolation be enforced below the application layer, and
satisfies it maximally: there is no application-layer `WHERE` clause to get wrong, because there is
no second tenant in the database to filter out.

### §2 There is no shared-infrastructure tier

The "Future SaaS" profile is **out of scope**. This ADR does not build toward it, reserve capacity
for it, or leave half-wired seams in its direction.

Introducing one is a decision of the same weight as this one and takes the same route: a **superseding
ADR** (`engineering-governance.md` §2 — "decision changes require a new/superseding ADR"), which must
answer the four objections in "Alternatives considered" below, and which must amend
`database-design.md`, `api-design.md` and `event-driven-architecture.md` *before* any code, because
none of the three documents defines a tenant column, a tenant header, or a tenant field in the event
envelope today.

### §3 The reserved tenant context stays reserved, and stays `None`

`platform/config.py` holds `tenant_id: ContextVar[UUID | None]`, documented by
`backend-implementation-guide.md` Part 8 as an extension point deliberately left unimplemented. It
keeps that status: **nothing sets it, nothing reads it, and under this ADR nothing ever will.**

This is pinned mechanically rather than by convention.
`tests/architecture/test_tenant_isolation.py` fails if any source file outside `platform/config.py`
references the tenant context, and if any migration introduces row-level security or a
`current_tenant` setting. The failure message names this ADR and the superseding-ADR requirement, so
the test is not an obstacle to a future shared tier — it is the thing that makes sure such a tier
arrives as a decision rather than as a merged pull request.

The alternative was deleting the ContextVar as dead code. It is kept because the guide documents it
and because a named, tested, permanently-inert seam is more honest than a silent absence: it tells
the next reader that single-tenancy is a decision, not an oversight.

### §4 Cross-agency sharing is explicit, audited and event-mediated

Unchanged from the draft. If two agencies must share intelligence, that is a deliberate export/import
flow between two deployments, audited on both sides — **never a shared table, a shared schema, or an
implicit join.** Nothing implements this today and this ADR does not add it; it fixes the shape any
future implementation must take, so that "let both agencies read one table" is off the table before
anyone proposes it under schedule pressure.

## Criticality Tier

**Tier 0 — Evidentiary/Crypto/Auth** (`engineering-governance.md` §3). Tenant isolation is an
authorization boundary and, through per-tenant key material (ADR-0009), a cryptographic one. A defect
here is a cross-agency evidence leak, which is unrecoverable: the disclosure cannot be undone and it
lands on data whose confidentiality is a legal obligation, not a product feature.

## Quality Gates checklist (§3)

| Gate | Result |
|---|---|
| 1 Lint & format | ruff clean |
| 2 Type check | `mypy --strict` clean |
| 3 Architecture validation | import-linter 2/2 contracts kept; the DAG is unchanged by this ADR |
| 4 ADR compliance | **This ADR is the compliance artifact.** No endpoint, event, table or field is introduced — the authoritative documents define no tenant column, header or envelope field, and this decision is the reason they still don't |
| 5 Unit tests + coverage | Tier 0 requires the invariant to be explicitly tested — `tests/architecture/test_tenant_isolation.py` |
| 6 Integration/contract tests | **Not applicable by construction, and this is the substantive point.** Proving isolation here means standing up two deployments and showing neither can reach the other's database — an infrastructure test against `deployment-architecture.md` Part 22, not a pytest against one Postgres. A pytest asserting that a single-tenant system does not leak between tenants it does not have would pass vacuously and prove nothing |
| 7 Security scans | Unchanged — no new code path |
| 8 Dependency scan | Unchanged — no new dependency |
| 9 Threat model | Below |
| 10 Performance benchmarks | Not applicable — no query path changes |

## Threat Model (Tier 0)

| Threat | Under this decision |
|---|---|
| **Cross-tenant read via a missing predicate** — the dominant failure mode of shared-table tenancy, where one forgotten `WHERE tenant_id = ?` discloses another agency's evidence | **Eliminated structurally.** There is no second tenant's data in the database to fail to filter |
| **Cross-tenant read via a bypassed policy** — RLS does not apply to a table owner, and does not apply to a role holding `BYPASSRLS` | **Not applicable.** No policy exists to bypass |
| **Connection-pool context bleed** — a tenant GUC set on a pooled connection outlives the request that set it, so the next request on that connection inherits the previous tenant's identity | **Not applicable.** No per-request database context is set |
| **Background work with no tenant to resolve** — the ADR-0006 dispatcher runs `FOR UPDATE SKIP LOCKED` on a schedule with no user, no request and no session to derive a tenant from | **Not applicable.** The worker's deployment *is* the tenant boundary |
| **Blast radius of a compromised deployment** | Contained to one agency: its own keys, its own storage, its own network zone. No credential recovered from one deployment authenticates to another |
| **Insider with database access** (§22's named threat: "a malicious or coerced insider") | Reaches one agency's data — the one that employs them and audits them. Under a shared tier the same credential would reach every customer's |
| **Operator error during deployment** — the realistic residual | A misconfigured deployment exposes *one* agency, and the exposure is a network/credential fact that `deployment-architecture.md` Part 21's egress verification and Part 24's checklist are built to catch. **Accepted as residual**, and it is a smaller residual than the shared-tier equivalent, where the same class of error is a cross-customer event |

**Residual accepted in writing:** this decision moves the isolation guarantee out of the application
and into deployment and operations. It is therefore only as strong as the deployment discipline in
`deployment-architecture.md`'s Mandatory Rules — GitOps-only changes, default-deny `NetworkPolicy`
per namespace, per-tenant secrets from Vault. Those rules were already mandatory; this ADR makes them
load-bearing for confidentiality, which is worth stating out loud rather than discovering during an
incident.

## Consequences

**Good.**

- The strongest isolation available, matching how this customer segment procures software, and it
  composes with the air-gapped profile (`security-architecture.md` §41) instead of fighting it.
- Per-tenant backup, restore, export, DR and key rotation are free — they are just *the deployment's*
  backup, restore, export, DR and key rotation.
- ADR-0003's hash-chained ledgers and ADR-0007's signed outbox stay single-agency by construction.
  This matters more than it first appears: under a shared table, two agencies' entries interleave in
  one hash chain, so extracting one agency's verifiable custody record for court means producing a
  structure whose integrity proof depends on rows belonging to an unrelated agency. Physical
  isolation makes a deployment's ledger a complete, self-contained evidentiary artifact.
- The codebase stays free of a tenancy abstraction that every future query, migration, index and
  event handler would otherwise have to honour correctly and forever.

**Costs, stated plainly.**

- **Per-agency operational cost is linear.** N agencies is N deployments, N upgrade windows, N
  restore drills. This is the real price and it is paid by DevOps, not by the codebase.
- **No SaaS offering**, and therefore no self-service small-customer segment. Recorded in
  `engineering-roadmap.md`'s technical-debt register as a deliberate, permanent trade-off rather than
  an unfinished item.
- **Scale ceiling is commercial, not technical** — dozens to low hundreds of agencies is a
  procurement and operations problem long before it is an engineering one.
- **Reversal is expensive.** See "Migration/rollback".

## Alternatives considered

**A. Shared tables with a `tenant_id` column, enforced by PostgreSQL Row-Level Security.** Rejected.
Four reasons, in descending order of weight:

1. **The customer base will not accept it** (§40) — it is not a cheaper control if the buyer's policy
   forbids relying on it.
2. **The isolation is weaker than it looks in this codebase specifically.** The platform runs one
   pooled async engine against one application role (`sentinel_app`/`sentinel_append`,
   `platform/db/privileges.py`). A `set_config('app.current_tenant', ...)` GUC therefore lives on a
   connection that outlives the request that set it, so correctness depends on a reset-on-checkin
   hook that is invisible at every call site and silently disclosive if it ever regresses. RLS also
   does not apply to a table owner and is waived entirely for `BYPASSRLS` — so the guarantee rests on
   role hygiene that no test in this repository would notice breaking.
3. **The ADR-0006 dispatcher has no tenant to bind.** It polls outbox tables on a lease with no user
   and no request, so it would need a bypass role — and a bypass role that touches every module's
   outbox is precisely the hole that makes RLS ceremonial.
4. **It contradicts the evidentiary model.** One hash chain over multiple agencies' entries, as
   above.

**B. Schema-per-tenant.** Rejected for now — but recorded as **the least-bad shared model** if §2 is
ever revisited, which is what the Proposed draft chose and why. It aligns with the existing
schema-per-module strategy, gives genuine per-tenant backup/export, and avoids objection A.1's
worst form. Its costs are that `search_path` manipulation has the same pooled-connection hazard as
A.2, that every one of the module Alembic histories must be applied per tenant in DAG order, and
that it scales to hundreds rather than tens of thousands of tenants. A superseding ADR that reopens
this should start here, not at A.

**C. Do nothing and leave the ADR Proposed.** Rejected. It had already blocked a phase-gate item
across two roadmaps, and an open tenancy question is an invitation for a shared-table `tenant_id` to
arrive incrementally in some unrelated increment, which is the outcome §40 exists to prevent.

## Migration/rollback

**Migration: none.** No schema change, no data migration, no Alembic revision. Every shipped
deployment already satisfies this ADR; the decision ratifies the status quo rather than moving to it.

**Rollback** of this *decision* is a superseding ADR, not a revert. What it would cost, so the cost is
known before anyone proposes it cheaply: a tenant discriminator added to every table in every module
schema plus `platform`; the tenant admitted to `api-design.md`'s request contract and to
`event-driven-architecture.md`'s signed envelope (a change to the signed field set, which is an
ADR-0007 concern); per-tenant key derivation under ADR-0009; and a re-answer to all four objections
in alternative A. The reason to write that down here is that the expensive half is the documents and
the crypto, not the `ALTER TABLE`.

## Supersedes / Superseded-by

**Supersedes:** nothing. This rewrites this ADR's own never-accepted `Proposed` draft, per ADR-0001.

**Superseded-by:** nothing. A future shared-infrastructure tier must arrive as ADR-00NN
"Shared-tier tenancy", naming this ADR in its `Supersedes:` line and leaving this one in place for
traceability.

## Traceability

**Up:** PRD **SR-7** (multi-tenant cryptographic/logical isolation — satisfied by having no
multi-tenant deployment to isolate) and **SR-8** (no cross-tenant AI model training without consent —
satisfied structurally, since `investigation`'s extraction operates inside one agency's deployment
and has no cross-agency corpus to train on). `security-architecture.md` §40, §51, §52.

**Down:** `platform/config.py`'s reserved `tenant_id` context;
`tests/architecture/test_tenant_isolation.py`; `deployment-architecture.md` Part 22's profile table;
`backend-implementation-guide.md` Part 8.

## Review sign-offs

Recorded honestly rather than ceremonially, because `engineering-governance.md` §2 requires Tier 0
verdicts from boards this team does not yet staff (§52 already concedes the equivalent about
incident-response roles):

- **Product decision (which profiles are in scope):** product owner, 2026-09-29 — physical isolation,
  no shared tier.
- **Architecture (ARB-equivalent):** lead architect, 2026-09-29.
- **Security (SRB-equivalent):** threat model above; no Critical or High finding, one residual
  accepted in writing.
- **Independent adversarial audit:** **not performed.** Governance requires one for a Tier 0 verdict.
  It is deliberately not blocking here, because the decision's effect is to *decline* to build the
  attack surface an audit would examine — but an audit is mandatory for any superseding ADR that
  introduces a shared tier, and that obligation is recorded in §2 above rather than left to memory.
