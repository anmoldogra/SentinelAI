# 3. Evidentiary Integrity: Authenticated, Externally-Anchored, Crypto-Agile Ledgers

## Status

**Accepted** (2026-09-08) — supersedes the integrity approach implied by
`canonical-evidence-model.md` §4 and `security-architecture.md` §22/§27.

Accepted on the **design**, which is now being built in dependency order as
`docs/modernization-roadmap.md` Wave 1. Acceptance is what unblocks that build; it is not a claim
that the subsystem exists. The implementation status below is verified against the code, not
assumed, and is expected to change — the Decision section is what is settled.

**Decided by this acceptance:**

- ⟨RESOLVED⟩ **Canonical encoding: RFC 8785 JCS**, not deterministic CBOR (RFC 8949 §4.2.1). JCS
  wins on the criterion that actually matters here — *independent* verifiability. A defence
  expert, an oversight body, or opposing counsel must be able to recompute an entry hash with
  tooling we did not write; JCS canonicalizes to ordinary JSON text, so any conforming
  implementation in any language reproduces the bytes, and the intermediate form stays readable
  in a report. Deterministic CBOR is more compact and marginally simpler to emit, but it puts a
  binary decode step between an auditor and the evidence, for a size saving that is irrelevant at
  ledger-entry scale. Implemented in `platform/crypto/canonical.py` (Wave 1.1).
- **Encoding version and `preimage_version` are separate axes.** The encoding names *how* a
  structure becomes bytes; `preimage_version` names *which fields* went in. Either can change
  without invalidating the other, and the Verification Engine dispatches on both.

**Still adopted defaults, pending security-board / FIPS ratification** for the specific deploying
agency (algorithm and anchoring mechanism may be constrained by air-gap or approved-algorithm
policy) — these remain genuinely open and are resolved in their own waves, not by this
acceptance:

- ⟨OPEN⟩ Signature algorithm: **Ed25519** (FIPS 186-5) + **SHA-256**, vs ECDSA P-256 — settled
  per deployment in Wave 1.2, which is when the first signature is actually produced.
- ⟨OPEN⟩ Anchoring: **RFC-3161 TSA + WORM**, with an internal transparency log and/or public
  blockchain anchor optional for deployments where a TSA is unreachable — settled in Wave 1.3.
  An air-gapped deployment has no reachable public TSA, so this cannot be closed generically.

**Implementation status (verified against the code as of 2026-09-08):**

| Decision | Wave | State |
|---|---|---|
| §2 Canonical encoding (JCS) | 1.1 | **Built** — `platform/crypto/canonical.py`, RFC 8785 vectors + JSONB round-trip tests |
| §5 Crypto-agility columns on both ledgers | 1.1 | **Built** — migrations `202609080001_platform_agility`, `202609080002_ingestion_agility`; all six columns, nullable |
| §4 Server-computed integrity hashing | 1.5 | **Built** — landed early (out of dependency order) as `09e4f14`; ADR-0008 §3 |
| §2 Complete preimage (all persisted fields) | 1.2 | **Built** — `platform/crypto/ledger.py`; both ledgers hash every persisted column under JCS and stamp `hash_algo`/`preimage_version`. Enforced by a table-driven test against the live schema |
| §1 Authenticated entries (**signatures**) | 1.2 | **Built** — `LedgerSigner` signs every audit and custody write under `KeyPurpose.EVIDENCE_ROOT` (Ed25519 by policy); `signature`/`sig_alg`/`key_id` carry real values. Fails closed: a write that cannot be signed aborts its transaction |
| §3 External anchoring — **Merkle + WORM** | 1.3 | **Built** — `platform/crypto/{merkle,anchoring}.py`, `platform.ledger_anchors`, COMPLIANCE-mode Object Lock. Truncation and rollback are detected; proven against a live database in `test_ledger_anchoring_db.py` |
| §3 External anchoring — **RFC-3161 timestamp** | 1.3 | **NOT built** — `tsa_token_ref` is reserved and null. WORM makes an anchor undeletable (which defeats truncation); a TSA makes it undatable-forward (which defeats backdating). Needs an ASN.1/CMS dependency and its own ADR |
| §6 Verification Engine | 1.4 | **Not built** — `LedgerSigner.verify` exists and is the primitive it will call, but no endpoint or scheduled job invokes it |

**Context §1's "unkeyed" half and Context §2 are now closed; "unanchored" is not.** A complete
preimage binds every field of an entry to its hash, catching an attacker who edits one row and
leaves the digest stale. A signature catches the attacker that hash could never catch: one who
edits a row *and recomputes its hash*, and every subsequent hash, exactly as the application
would. That forgery is now detectable because producing a valid signature requires the private
key, which lives in the KMS and to which the application's database role has no path.
`tests/integration/test_ledger_signatures_db.py` performs that attack against a live database and
asserts the verification failure, rather than asserting the property in prose.

**What is still open, and it is not a footnote.** Signatures make *edits* detectable. They do
nothing about **truncation or rollback**: an insider who deletes the last N entries, or restores
an older backup, leaves a shorter chain in which every remaining entry still verifies perfectly.
Nothing in the database can detect that, because the evidence of the missing entries is exactly
what was removed. Only §3's externally-anchored monotonic root closes it, and that is Wave 1.3.

**PRD SR-4 now holds against modification *and* removal, with one caveat.** Signatures make an
altered entry detectable; WORM-published Merkle roots make a deleted one detectable, because the
commitment lives outside the database the attacker controls. The remaining caveat is **time**: with
no RFC-3161 token, an attacker holding both the application and the clock could publish a fresh
anchor over a doctored history. They cannot replace an anchor *already* in WORM, so this is a
narrow residual rather than the open hole truncation was. The honest description today is
**"tamper-evident and non-repudiable against modification and removal; not yet proof against
backdating"**.

**One consequence to carry forward.** Signing happens inside the caller's transaction and fails
closed, so a KMS outage stops every audited write rather than allowing an unsigned one. That is
the correct trade for a legal record, but it makes the KMS a hard availability dependency of the
whole write path. Batched Merkle signing (Wave 1.3) is the mitigation this ADR's Consequences
already anticipated.

## Context

The current custody (`ingestion.evidence_custody_events`) and audit
(`platform.audit_log`) ledgers are **bare, unkeyed SHA-256 hash chains** whose head
hash is stored in the same writable database, with no signing key, no external anchor,
no verification routine, and no database-level append-only enforcement. Verified defects:

1. **Unkeyed & unanchored** (`platform/auth/audit.py:_compute_hash`,
   `modules/ingestion/service.py:_custody_entry_hash`): a privileged writer (hostile DBA,
   compromised app, malicious insider) can rewrite any entry and recompute every
   subsequent hash to produce a chain that verifies. The genesis anchor is the public
   constant `"0"*64`. There is no signature (grep: no `hmac`/`sign`/`kms`/`hsm`).
2. **Incomplete preimage.** Audit hashes only `{prev, action, target_id, details}` —
   omitting `occurred_at`, `actor_user_id`, `actor_role`, `module`, `target_type`,
   `ip_address`, `user_agent`. Custody omits `actor_user_id`, `actor_role`,
   `authority_ref`, `notes`. The forgeable fields are exactly the attribution fields.
3. **Rollback/forking undetectable.** With no externally-anchored monotonic root, a
   routine backup-restore silently erases later evidence and still verifies.
4. **Client-declared integrity hash.** For `payload_ref` evidence,
   `integrity_hash_at_event` is the client's asserted hash, never recomputed from stored
   bytes; `integrity_verification_status` is set `pending` and never advances.
5. **No crypto agility.** No `hash_algo`/`preimage_version`/`sig_alg` columns — the
   integrity format cannot evolve over a 10–15 year horizon without invalidating history.

This fails PRD SR-4 ("tamper-evident even to an administrator with direct database
access") and is not court-defensible against an insider. **There is no production
evidence yet (Alpha, never executed), so this is the cheapest it will ever be to fix —
a greenfield redesign with near-zero backfill cost.**

## Decision

Evidentiary ledgers become **authenticated, externally-anchored, crypto-agile** records:

1. **Authenticated entries (not bare hashes).** Each custody/audit entry stores
   `entry_hash = H(canonical_encoding(all evidentiary fields) || prev_entry_hash)` **and**
   a `signature` over `(sequence || prev_entry_hash || entry_hash)` produced by an
   asymmetric key held in a KMS/HSM the application's DB role cannot read (see ADR-0009
   Key Management). Verification is signature-based, so a writer without the key cannot forge.
   - Signature algorithm: **Ed25519** (FIPS 186-5) is the implemented policy default; ECDSA P-256
     remains selectable per the deploying agency's FIPS/HSM posture, as a configuration change
     rather than a code change (ADR-0009 §3 — callers never name an algorithm). Hash: SHA-256 (or
     SHA-384 for higher assurance). The per-deployment ratification noted in Status is unchanged.
2. **Complete, versioned canonical encoding.** All persisted evidentiary fields are
   covered. Encoding is deterministic and independent of JSONB round-trips: **RFC 8785 JCS**
   — resolved at acceptance in favour of independent verifiability over deterministic CBOR
   (RFC 8949 §4.2.1); see Status. A `preimage_version` column pins the **field set**, and the
   encoding carries its own version, so the two evolve independently.
3. **External trust anchoring.** Periodically (e.g., per N entries or per interval) build a
   **Merkle tree** over new entries, sign the root, and anchor it via **RFC-3161 timestamping
   (TSA)** written to **WORM** storage — and ⟨OPEN⟩ optionally an internal **transparency
   log** and/or public blockchain anchor for air-gapped-incompatible deployments. Anchored,
   monotonic roots make rollback, truncation, and forked histories detectable.
4. **Server-computed integrity hashing.** Ingest streams the payload and computes the
   integrity hash server-side; a client-declared hash is compared, never trusted as
   authoritative; `integrity_verification_status` transitions to `verified`/`failed`.
5. **Crypto agility.** New columns on both ledgers: `hash_algo`, `sig_alg`, `key_id`,
   `preimage_version`, `signature`, `anchor_ref`. The Verification Engine dispatches by
   version so historical entries remain verifiable after algorithm/key rotation.
6. **Verification Engine.** A first-class subsystem: (a) an online endpoint returning a
   court-facing verification report for an evidence item's full chain; (b) a scheduled job
   that re-verifies chains, signatures, and anchor roots and alarms on any break.

## Amendment (2026-09-08) — `anchor_ref` cannot live on the ledger rows

§5 lists `anchor_ref` among the agility columns on both ledgers. **That placement is not
implementable, and the conflict is with ADR-0004.** An anchor necessarily exists *after* the
entries it commits to — a Merkle root cannot be computed over rows that have not been written — so
recording it on those rows would require an `UPDATE`. ADR-0004's append-only trigger rejects
`UPDATE` on exactly these tables, unconditionally.

Resolved in favour of ADR-0004: **never weaken append-only to make bookkeeping convenient.** The
entry→anchor relationship lives in a separate append-only table, `platform.ledger_anchors`, which
records the covered range by entry hash. The `anchor_ref` columns on both ledgers remain
permanently `NULL`; they are left in place rather than dropped so a verifier meeting an old row
knows the column was never populated, and because dropping a column from an evidentiary table is
itself a schema change on a signed history.

One table serves both ledgers, discriminated by a `ledger` column carrying the same value the
signed message uses. Anchoring is generic over what it commits to, and duplicating the table into
`ingestion` would duplicate the verification code with it. There is no foreign key to either chain:
`ingestion` is another module's schema and `database-design.md` §5 forbids cross-schema FKs, so the
reference is by entry hash and validated at the application layer.

**A second correction to §1's wording.** The ADR specifies the signed message as
`(sequence || prev_entry_hash || entry_hash)`. Implemented as a raw concatenation that is ambiguous
the moment crypto agility does its job — a SHA-256 digest is 64 hex characters and a SHA-384 digest
is 96, so `prev || entry` stops being uniquely parseable once two algorithms coexist. It is built as
an RFC 8785 canonical object over the same fields, plus a ledger discriminator so a signature made
for one chain cannot be presented as valid for the other.

## Consequences

- **Gains:** non-repudiation against a hostile DBA/insider; rollback/forking detection;
  server-verified payload integrity; court-defensibility; a format that can evolve for 15 years.
- **Costs:** per-entry signing latency (mitigated by batched Merkle signing and async
  anchoring); KMS/HSM operational dependency (ADR-0009); schema additions on both ledgers;
  a new Verification Engine to build and operate.
- **Depends on:** ADR-0004 (append-only DB protections), ADR-0009 (KMS abstraction),
  ADR-0008 (storage architecture / WORM). Blocks nothing else structurally — the module,
  event, and persistence architecture are unchanged.
- **Migration:** greenfield (no evidentiary data exists). Column additions via new
  migrations; no backfill. Immutable once real evidence is written — hence do this first.
