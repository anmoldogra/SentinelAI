# 3. Evidentiary Integrity: Authenticated, Externally-Anchored, Crypto-Agile Ledgers

## Status

Proposed — supersedes the integrity approach implied by `canonical-evidence-model.md` §4
and `security-architecture.md` §22/§27. The ⟨OPEN⟩ items below are resolved to the
**adopted defaults** noted inline, **pending security-board / FIPS ratification** for the
specific deploying agency (algorithm and anchoring mechanism may be constrained by air-gap
or approved-algorithm policy).

**Adopted defaults (pending ratification):** signatures **Ed25519** (FIPS 186-5) + **SHA-256**;
canonical encoding **RFC 8785 JCS**; anchoring **RFC-3161 TSA + WORM** (internal transparency
log optional).

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
   - ⟨OPEN⟩ Signature algorithm: **Ed25519** (FIPS 186-5) vs **ECDSA P-256** — choose per
     the deploying agency's FIPS/HSM posture. Hash: SHA-256 (or SHA-384 for higher assurance).
2. **Complete, versioned canonical encoding.** All persisted evidentiary fields are
   covered. Encoding is deterministic and independent of JSONB round-trips —
   ⟨OPEN⟩ **RFC 8785 JCS** vs **deterministic CBOR (RFC 8949 §4.2.1)**. A `preimage_version`
   column pins the format.
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
