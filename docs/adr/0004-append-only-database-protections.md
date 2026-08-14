# 4. Append-Only Database Protections for Evidentiary Tables

## Status

Proposed. Prerequisite for ADR-0003 (defense-in-depth beneath the signed/anchored ledgers).

## Context

`ingestion.evidence`, `ingestion.evidence_custody_events`, and `platform.audit_log` are
ordinary writable tables — verified: no `GRANT/REVOKE`, no triggers, no INSERT-only role in
any migration. `security-architecture.md` §27 mandates INSERT-only permissions for
custody/audit. Today the application role can `UPDATE`/`DELETE` any evidentiary row, and
`evidence.legal_hold` is a boolean flippable by a single `UPDATE`. Even with ADR-0003's
authenticated chain, prevention-in-depth is required so that (a) accidental mutation is
impossible and (b) a compromised app role cannot even attempt a silent edit.

## Decision

1. **Role separation.** Three PostgreSQL roles: `sentinel_migrator` (owns DDL; used only by
   Alembic PreSync jobs), `sentinel_app` (DML on mutable module tables), and
   `sentinel_append` (INSERT + SELECT only on evidentiary tables). The application connects
   with `sentinel_app`+`sentinel_append`; it never holds DDL or `UPDATE`/`DELETE` on
   evidentiary tables.
2. **Hard privilege revocation.** On `evidence`, `evidence_custody_events`, `audit_log`:
   `REVOKE UPDATE, DELETE` from all application roles; `GRANT INSERT, SELECT` only.
   (`evidence.status` transitions — e.g. `validated→superseded` — move to an append-only
   supersession model rather than an in-place `UPDATE`; see Consequences.)
3. **Trigger backstop.** `BEFORE UPDATE OR DELETE` triggers on the three tables that
   `RAISE EXCEPTION`. A superuser can drop triggers — that residual risk is covered by
   ADR-0003's external anchoring, not by the DB alone.
4. **Legal hold becomes ledger-derived.** Replace the mutable `evidence.legal_hold` boolean
   semantics with state **derived from the custody ledger** (`legal_hold_applied` /
   `legal_hold_released` events); the boolean, if retained, is a trigger-maintained read cache
   that the app cannot write directly.
5. **Migrations only via `sentinel_migrator`**, run as ArgoCD PreSync (per
   `deployment-architecture.md`), never by the app.

## Consequences

- Accidental and most malicious in-place mutation of evidentiary data becomes impossible at
  the DB layer; combined with ADR-0003, tampering is both prevented and detectable.
- `evidence` status changes must be modeled as new rows/events, not `UPDATE`s — this tightens
  the domain toward true append-only and aligns with CEM §12 supersession.
- Operational cost: role/connection management; migration tooling must assume the restricted
  app role; some ORM conveniences (dirty-attribute UPDATE) are forbidden on these tables by design.
- Migration: additive GRANT/REVOKE + trigger migrations; must land before any real evidence.
