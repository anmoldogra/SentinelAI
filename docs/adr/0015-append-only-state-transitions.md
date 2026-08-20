# 15. Evidence State Transitions Are Derived, Never Written In Place

## Status

Proposed. Resolves implementation-log IC-003 (the ADR-0004 ↔ ADR-0008 §3 / ADR-0003 §4
conflict). Depends on ADR-0004 (append-only protections); consumed by ADR-0008's
scan/promote flow and ADR-0003's Verification Engine.

## Context

Three fields on `ingestion.evidence` are conceptually mutable while the table is physically
append-only:

- `status` — `validated → superseded` (CEM §12 supersession);
- `legal_hold` — applied/released over the item's life (security-architecture §39);
- `integrity_verification_status` — `pending → verified/failed` (ADR-0008 §3, ADR-0003 §4).

ADR-0004 installs a `BEFORE UPDATE OR DELETE … RAISE` trigger and `REVOKE UPDATE, DELETE`
on `ingestion.evidence` with no column carve-out, so **no** in-place transition can ever
execute. IC-003 verified the conflict and found three pre-existing service-code sites that
attempted in-place mutation (`legal_hold` ×2, `status` ×1) and would fail against a real
database; unit tests pass only because fakes don't enforce the trigger.

Candidate resolutions evaluated:

1. **Weaken the trigger** (column-level carve-out for the three fields). Rejected: it
   converts "tamper-evident even to an administrator" into "tamper-evident except for the
   three most legally consequential fields". A flipped `legal_hold` or a forged `verified`
   is precisely what ADR-0004 exists to prevent.
2. **New append-only `evidence_state_transitions` table.** Rejected: it duplicates the
   custody ledger. Every transition this table would record — hold applied/released,
   integrity checked, superseded — is *already* an event the custody ledger records (or an
   existing row linkage). Two append-only records of the same fact can disagree, and the
   one that wins in court must be the hash-chained, soon-to-be-signed (ADR-0003) custody
   ledger. A second table adds a write, a table, and an ambiguity while adding no fact.
3. **Hash synchronously at ingest** so rows are born `verified`. Rejected: streams multi-GB
   images inside an HTTP request (ADR-0008's context forbids this) and does not address
   `legal_hold` or `status` at all.

## Decision

**Evidence operational state is *derived* from append-only records at read time; the three
columns store only their INSERT-time (genesis) values and are never UPDATEd.**

1. **`status`** is derived from CEM §12 supersession structure: an item is `superseded`
   iff a replacement row exists with `supersedes_evidence_id = <its id>`; otherwise its
   genesis value (`validated`) stands. No write to the original row occurs on supersession.
2. **`legal_hold`** is derived from the custody ledger (exactly ADR-0004 §4): the latest
   `legal_hold_applied` / `legal_hold_released` event wins; with no such event, the genesis
   value (`false`) stands.
3. **`integrity_verification_status`** is derived from the custody ledger: the latest
   `integrity_reverified` event's `integrity_hash_at_event` (the server-recomputed digest,
   ADR-0008 §3) is compared — constant-time — against the row's recorded `integrity_hash`;
   equal → `verified`, unequal → `failed`; no such event → genesis value (`pending`, or
   `not_applicable` for non-payload evidence). The comparison is semantic (digest
   equality), never a parse of event notes.
4. **The service applies the derived state as a read-time overlay** on the ORM instance
   using SQLAlchemy's `set_committed_value` — populating attributes *as if loaded*, so the
   unit of work never marks them dirty and never emits an `UPDATE`. Response schemas and
   all in-service checks (legal-hold gates, already-superseded guards) therefore see
   derived truth with no schema or API change.
5. **No schema change, no migration, no trigger change.** The columns remain (genesis
   values are still meaningful data); the ADR-0004 protections remain exactly as installed.

## Consequences

- The ADR-0004 guarantee is preserved in full: `ingestion.evidence` and the custody ledger
  remain physically immutable; a hostile writer cannot flip a hold or forge a verification
  without appending a ledger event that ADR-0003 will sign and anchor.
- One source of truth: the custody ledger (plus the supersession row linkage) is the only
  record of state changes — the same record the court sees.
- Read cost: deriving state costs up to three indexed queries per item (`EXISTS` on the
  self-referencing FK, two `LIMIT 1` ledger lookups). Acceptable at Phase 1 scale; if list
  endpoints become hot, the sanctioned optimization is a SQL derivation (lateral join /
  view) or ADR-0004 §4's trigger-maintained read cache — **not** a return to in-place
  UPDATEs. The `status` list filter is already pushed down as an `EXISTS` subquery.
- The service-layer contract tightens: **no code may assign to `status`, `legal_hold`, or
  `integrity_verification_status` on a persistent `Evidence` instance.** In-memory
  consistency after a transition is maintained via `set_committed_value` only.
- Database contents alone no longer show current state for these three fields (they show
  genesis); any direct-SQL consumer must join the derivation. This is deliberate — the
  ledger is the record — and is documented in `database-design.md`.
