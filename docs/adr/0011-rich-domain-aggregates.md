# 11. Rich Domain Aggregates for the Evidentiary Core

## Status

Proposed. Depends on ADR-0005 (transaction boundary).

## Context

The domain model is anemic: ORM rows carry no behavior, and every invariant — evidence
write-once, custody monotonicity + hash linkage, case/finding state machines, the CEM §13
"≥1 supporting evidence" rule — is enforced only inside service methods. A single code path or
raw SQL statement that bypasses the service silently violates a **legal** invariant. Tactical
DDD (aggregates, value objects, domain events) is absent despite the DDD framing.

## Decision

1. **True aggregates own their invariants:**
   - `Evidence` — owns its custody ledger and integrity/supersession rules; illegal mutation is
     impossible because the aggregate exposes only `record_custody(...)`, `supersede(...)`,
     `apply_legal_hold(...)` and refuses invalid transitions.
   - `Case` — owns the `open→closed→archived` machine.
   - `Finding`/`Relationship` — owns `proposed→confirmed|rejected` and the ≥1-supporting-evidence
     invariant.
2. **Value objects** for CEM concepts with validation at construction: `IntegrityHash`,
   `ConfidenceScore` (0–1), `EvidenceCategory`/`ArtifactType`, `LegalAuthorityRef`,
   `CustodyEventType`.
3. **Domain events vs integration events made explicit in code** (event-driven §4): aggregates
   raise domain events; the application layer maps the curated subset onto the outbox.
4. **Belt-and-suspenders:** aggregate-enforced invariants sit on top of ADR-0004's
   database-enforced append-only — an invariant is protected at both layers.

## Consequences

- Invariants become structurally hard to violate; the domain is far more testable (aggregate
  unit tests without a DB) and readable.
- Adds an aggregate↔ORM mapping layer and a migration of the three implemented modules to the
  aggregate pattern (moderate, mechanical).
- Slightly more indirection; justified precisely because these invariants are legal guarantees,
  not conveniences.
