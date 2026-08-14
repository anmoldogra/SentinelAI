# SentinelAI Phase-II Modernization — Master Implementation Roadmap

Dependency-ordered plan to take the backend from **Alpha** to a **court-defensible,
production-grade** platform. Each subsystem is implemented one at a time (never mixed),
gated on its ADR. Sequencing is by dependency, not calendar.

> **Non-negotiable gate:** the evidentiary core (Wave 0 + Wave 1) MUST land before any real
> evidence is ever written. It is greenfield now (no production data) and therefore the
> cheapest it will ever be to get right.

Legend — Complexity: S / M / L / XL. Breaking: schema / API / internal.

## Wave 0 — Cryptographic & Storage Foundation
Nothing evidentiary is trustworthy until these exist.

| # | Subsystem | ADR | Prereqs | Complexity | Breaking | Migration | Rollback | Testing | Docs |
|---|---|---|---|---|---|---|---|---|---|
| 0.1 | **KMS/HSM abstraction** | 0009 | — | L | internal | none (new port) | feature-flag to dev backend | port unit tests; sign/verify/rotate vectors; Vault/HSM integration test | ADR-0009; security-architecture key-mgmt §; ops runbook |
| 0.2 | **Append-only DB roles + triggers** | 0004 | — | M | schema (roles/grants) | new GRANT/REVOKE + trigger migrations; role split | drop triggers / restore grants | migration up/down; negative tests (UPDATE/DELETE must fail) | ADR-0004; database-design §27 update |
| 0.3 | **Object storage + streaming hash + WORM + quarantine** | 0008 | 0.1 | L | internal | new buckets + Object Lock; no table change | disable promote; keep quarantine | streaming-hash vectors; scan-flow integration; large-object test | ADR-0008; security §24–26 |

## Wave 1 — Evidentiary Integrity Core (THE GATE)

| # | Subsystem | ADR | Prereqs | Complexity | Breaking | Migration | Rollback | Testing | Docs |
|---|---|---|---|---|---|---|---|---|---|
| 1.1 | **Canonical encoding (JCS) + agility columns** | 0003 | — | M | schema | add `hash_algo,sig_alg,key_id,preimage_version,signature,anchor_ref` to custody + audit | additive; drop columns | canonical-encoding determinism/property tests; JSONB round-trip guard | ADR-0003; CEM §4 update |
| 1.2 | **Complete signed preimage (custody + audit)** | 0003 | 0.1, 1.1 | M | internal | none (new writes) | revert to prior hash fn | preimage covers ALL persisted fields (table-driven test); forgery-detection test | CEM §4; security §22 |
| 1.3 | **Merkle-root external anchoring (RFC-3161 + WORM)** | 0003 | 0.1, 0.3, 1.2 | L | internal | anchor-store bucket | disable anchoring job | rollback-detection test; anchor-verify test | ADR-0003 |
| 1.4 | **Verification Engine (endpoint + scheduled job)** | 0003 | 1.2, 1.3 | L | API (adds `/verify` report) | none | disable job | chain/signature/anchor verify tests; tamper-injection detection | ADR-0003; api-design new § verification |
| 1.5 | **Server-computed integrity on ingest** | 0003 | 0.3 | M | internal | none | trust-client fallback (dev only) | client-vs-server hash mismatch → reject | ADR-0008/0003 |

## Wave 2 — Distributed & Domain Correctness

| # | Subsystem | ADR | Prereqs | Complexity | Breaking | Migration | Rollback | Testing | Docs |
|---|---|---|---|---|---|---|---|---|---|
| 2.1 | **Request-scoped transaction boundary** | 0005 | — | M | internal | none | revert DI wrapper | atomicity tests (multi-service rollback); batch savepoint tests | ADR-0005; backend-guide Part 3 update |
| 2.2 | **Dispatcher → worker, SKIP LOCKED, ordering** | 0006 | 2.1 | L | internal | index `(dispatch_status,aggregate_id,occurred_at)` | run in HTTP (old path) | concurrent-dispatch dedup test; per-aggregate ordering test | ADR-0006; event-driven §2/§18 |
| 2.3 | **Event authentication (signed outbox)** | 0007 | 0.1, 2.2 | M | schema | add `signature,key_id,sig_alg` to outbox shape | verify-optional flag | forged-event rejection; verify-before-inbox test | ADR-0007; event-driven §9 |
| 2.4 | **Rich aggregates + value objects** | 0011 | 2.1 | XL | internal | none | keep service-logic path | aggregate invariant unit tests (no DB) | ADR-0011; CEM value objects |

## Wave 3 — Access & API Trust

| # | Subsystem | ADR | Prereqs | Complexity | Breaking | Migration | Rollback | Testing | Docs |
|---|---|---|---|---|---|---|---|---|---|
| 3.1 | **Authentication + sessions (fix D1) + `case_members`** | 0010 | 0.1 | XL | schema + API | add `sessions.token_hash`, `case_members`; auth/admin routers | feature-flag auth (dev bypass) | login/MFA/SSO; RBAC/ABAC enforcement; revocation | ADR-0010; api-design §9–10; database-design §3.1/§3.4 |
| 3.2 | **API idempotency store** | 0012 | 2.1 | M | schema | add `platform.idempotency_keys` | disable middleware | retry-dedup test; fingerprint-conflict 422 | ADR-0012; api-design §2.9 |

## Wave 4 — Scale, Multi-Agency, Operations (Phase 2)

| # | Subsystem | ADR | Prereqs | Complexity | Breaking | Notes |
|---|---|---|---|---|---|---|
| 4.1 | **CQRS / graph read models** | 0013 | 2.2 | XL | internal | benchmark-gated graph-store decision |
| 4.2 | **Multi-tenancy** | 0014 | 3.1 | XL | schema | product decision on profiles first |
| 4.3 | **Observability** (OTel traces/metrics/logs, alerting) | — | 2.2 | L | internal | wire the trace_id already in the envelope |
| 4.4 | **DR / backup + independent integrity attestation** | 0003/0004 | 1.4 | L | ops | anchored roots enable DR integrity proofs |
| 4.5 | **Redpanda transport** | — (event-driven §Phase-3) | 2.2, 2.3 | L | internal | signatures + envelope already transport-independent |

## Definition of Done (every subsystem)
Implementation · migration (up + real `downgrade`) · unit + integration tests · security review vs OWASP ASVS / NIST · benchmark where latency-relevant · ADR marked **Accepted** · affected design docs updated in the same change · import-linter + type-check + `make check` green.

## Strengths preserved throughout (must not regress)
Module isolation + `public.py` + import DAG · schema-per-module + no cross-schema FK · outbox/inbox effective-once · canonical evidence model · uniform per-module layout.
