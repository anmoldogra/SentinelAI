# 9. Key Management, Cryptographic Trust Boundary & Signature Model

## Status

**Accepted (architecture) — revised.** This revision **supersedes and replaces** the previously
"Accepted & implemented" ADR-0009 in its entirety (do not read the prior text; it is retired).
The architecture below is approved with **no remaining Critical findings** (see "Security Review
Board — Verdict"). Marking the *implementation* **FINAL** is gated on one execution-validation
wave (Wave 0.1.f) against real dependencies and a live Vault; that gate does **not** block
designing ADR-0004, but does block trusting the code in production.

Companion operational reference: `docs/kms-key-hierarchy-and-ops.md`. Foundation for ADR-0003
(evidentiary anchoring), 0007 (event authentication), 0008 (object/column encryption), 0010
(token/secret keying).

## Context

SentinelAI is intended as a national-scale digital-forensics, intelligence, investigation, and
cyber-operations platform with a **20+ year** evidentiary horizon, multi-agency deployment, and
court-admissibility requirements. Every downstream cryptographic guarantee — chain of custody,
event authenticity, encryption-at-rest, session security — rests on one trust boundary. If that
boundary's *metadata* is forgeable, its *keys* are lost, or its *history* becomes unverifiable
after a provider or algorithm change, all evidence beneath it is compromised.

An independent adversarial audit of the first implementation found one Critical defect (an
unauthenticated signature envelope) and several High/Medium defects (no Vault lease lifecycle, no
resilience, non-atomic dev keystore, unsafe cross-provider verification fallback, default-credential
acceptance). This revision resolves the **model** behind those defects — modelling *time* (20-year
verifiability), *distance* (network RPC to a quorum system), and *adversary* (all metadata is
attacker-controlled until signed) — not merely their symptoms.

No production key material exists yet; the design is greenfield, so migration cost is near zero.

## Decision

### D1. Provider-agnostic trust boundary

All cryptography funnels through `platform.crypto`. Consumers use a `KeyManagementService`
facade with a logical `KeyRef(purpose, name)` and **never** name an algorithm, provider, key
version, or handle private key material. Providers implement a `CryptoProvider` port and are
config-selected: `dev` (software, **forbidden in production, fails closed**), `vault_transit`,
and registered slots `aws_kms` / `azure_key_vault` / `gcp_kms` / `pkcs11`. No provider-specific
type or logic escapes `platform.crypto`. `platform.crypto` imports **no** domain module.

### D2. Self-authenticating signature envelope (the core evidentiary control)

The unit that is signed is a **canonical `SignedHeader`**, serialized as JCS-style deterministic
JSON, binding every trust-bearing field:

```
canonical_header_version   -- schema version of this header (dispatch key)
signature_version          -- envelope/algorithm-suite version
algorithm                  -- e.g. Ed25519
key_id { provider, ref, version }
key_purpose                -- ROOT_TRUST | EVIDENCE_ROOT | EVENT_ROOT | ...
required_algorithms[]       -- the set a valid bundle MUST contain (downgrade defense)
payload_hash               -- hash of the payload; payload is NEVER embedded
payload_hash_algorithm
created_at                 -- advisory time (see D9)
```

Providers sign/verify **raw bytes only**; the facade owns the envelope. Because the payload is
referenced by hash, arbitrarily large evidence never enters a signature. Every metadata field is
therefore tamper-evident: editing any of it invalidates the signature.

### D3. Signature bundle & hybrid PQC model

`sign()` returns a `SignatureBundle` of ≥ 1 `Signature`. A bundle is **valid iff**:

1. every member verifies over its own `SignedHeader.canonical_bytes()`;
2. all members share one identical `required_algorithms` set;
3. `required_algorithms ⊆ present_algorithms` (**downgrade / stripping defense**).

**Post-quantum migration is a governed 3-stage ceremony**, with no envelope or API change:
(i) add the PQC algorithm to `required_algorithms` and **dual-sign** (classical + PQC); (ii) hold
dual-sign until all verifiers are upgraded; (iii) after a published sunset date, drop the classical
algorithm from `required_algorithms`. A configured-but-unsatisfiable required-set (e.g. hybrid
requested with no PQC-capable provider) **fails closed at sign time** — never a silent single
signature.

### D4. Verification pipeline (fixed, ordered, fail-closed)

`payload_hash match → per-signature cryptographic verify over header bytes → bundle required-set
consistency → required ⊆ present → (future) revocation / validity-window checks`. Any failure ⇒
reject. No partial trust, ever. New checks are inserted into this ordered gate without touching
call sites.

### D5. Algorithm policy engine + capability negotiation

Callers never name algorithms; an `AlgorithmPolicy` resolves `KeyPurpose → algorithm(s)` from
configuration (default **Ed25519**; ECDSA-P256, RSA-PSS, ML-DSA/Dilithium are named policy
targets). Each provider advertises `ProviderCapabilities`
(`supports_signing/encryption/data_keys/rotation/attestation/export_public_key` + algorithm set).
The **policy ∩ capability** resolution happens at **startup** and is logged: an impossible
policy/provider pairing fails at boot, not at first sign.

### D6. Provider abstraction & cross-provider verification (long-horizon verifiability)

`key_id` encodes the `ProviderKind`. The **verification provider set** is the config-pinned union
of *every provider ever activated in this deployment*. `provider_for(key_id)` resolves **strictly
by `key_id.provider` and fails closed (`ProviderNotAvailable`) if that provider is not registered —
it never falls back to a default provider.** Decommissioning a provider is a **governed migration**
(re-anchor / re-sign affected evidence via ADR-0003, or retain the old provider in verify-only
mode), never a silent config removal. This preserves verifiability across Vault→HSM→cloud-KMS moves
over the platform's multi-decade life.

### D7. Auditable key lifecycle

`create / enable / disable / archive / rotate / destroy / list_versions / get_metadata`; each
lifecycle op emits an audit event via an injectable `AuditSink` (structured logging now;
hash-chained ledger later, no coupling to `platform.auth`). **Read/verify paths are ungated** —
a disabled/archived key still verifies historical evidence forever; only signing/encryption is
gated. `destroy` is **forbidden** on any key that has ever signed evidence (tombstone only).

### D8. Key hierarchy, durability, DR & escrow

Hierarchy: `Root Trust → {Evidence Root, Event Root, Storage Root, Session Root} → Derived data
keys`. The application **never holds exportable private keys**, so durability is the backend's
responsibility: Vault Integrated Storage (Raft) + snapshot/DR replication, cloud-KMS multi-region
durability, or an HSM key-backup ceremony to a second HSM. The **root-trust** key uses **M-of-N
Shamir escrow** with custodians in separate custody. **Old key versions are retained forever for
verification** (rotation never destroys history). Full runbook (backup, DR promotion, escrow,
recovery, ceremony, rotation retention) in `docs/kms-key-hierarchy-and-ops.md`.

### D9. Time is advisory here; authoritative time is ADR-0003

`created_at` inside the header is **tamper-evident but not trusted time** — it is application wall
clock. Court-grade timestamps come from ADR-0003's external RFC-3161 anchoring. This boundary is
stated explicitly so no consumer mistakes KMS `created_at` for a trusted timestamp.

### D10. Vault authentication lifecycle, resilience & health

AppRole (short-lived leases) is preferred; static token supported for lower environments. A
renew-self loop renews leases; auth is re-established gracefully; **lease loss sets health
UNAVAILABLE ⇒ `/readyz` 503 (fail closed)**. Provider RPCs use bounded exponential backoff with
full jitter, a per-call timeout, and a per-provider circuit breaker (closed/half-open/open), all
metered. Thresholds are tuned against provider SLOs. `HealthStatus` (`READY/DEGRADED/UNAVAILABLE`)
feeds `/readyz`, OpenTelemetry, and Prometheus.

### D11. Security invariant & fail-closed configuration

No private key or plaintext data key ever appears in memory dumps, exceptions, tracebacks, logs,
serialization, debug endpoints, or tests; secret-bearing value objects redact their `repr`;
fixtures use ephemeral keys only. In production, a **default/placeholder Vault credential fails
closed at construction**; AppRole in production requires role-id + secret-id. The dev software
provider raises at construction if instantiated in production.

## Threat model

| Threat | Detected / Prevented / Recovered | Mechanism |
|---|---|---|
| Insider (DBA/dev) | Prevented + Detected | keys never in DB/app memory; content edits fail verify; lifecycle audited |
| Vault compromise | Contained + Recovered | least-privilege AppRole scoped to `transit/*`; non-exportable keys; DR promotion + credential rotation |
| Provider compromise | Contained | can sign, cannot exfiltrate keys; policy/capability caps algorithms; governed decommission |
| Replay | Deferred to ADR-0007 (Inbox/idempotency); header binds payload+time as an input | consumers dedupe on `event_id` |
| Downgrade | Prevented | `required ⊆ present`, required-set is signed |
| Metadata tampering | Prevented | all metadata inside the signed header |
| Bundle stripping | Prevented | dropping a required member ⇒ required ⊄ present |
| Algorithm substitution | Prevented | `algorithm` is signed |
| Token theft | Mitigated + Recovered | short-lived leases; renewal; revoke+rotate; fail-closed on loss |
| Key compromise | Recovered | rotate; disable version for signing; retain for verify; re-anchor via ADR-0003 |
| Key destruction | Prevented + Recovered | `destroy` forbidden on evidence-bearing keys; M-of-N escrow + Raft/DR restore |
| DR failure | Recovered | exercised DR promotion runbook; post-recovery chain re-verification |
| Long-term crypto deprecation | Managed | agility metadata + versioned envelope + policy engine |
| Quantum migration | Managed | hybrid dual-sign ceremony; downgrade-proof required-set |

Accepted residuals (non-critical, tracked): trusted time is ADR-0003's responsibility (D9); the
dev keystore is single-process by design (production uses Vault/HSM).

## Implementation roadmap (Wave 0.1)

Small waves; each has effort / dependencies / risk / rollback / validation / DoD.

- **0.1.a Envelope & pipeline (C1)** — canonical `SignedHeader`, bundle rules, ordered verify.
  *Effort* S. *Deps* none. *Risk* Low (greenfield). *Rollback* revert commit.
  *Validation* unit: roundtrip, tamper, downgrade-strip, agility metadata. *DoD* all four green.
  **(Code already written; awaits execution in 0.1.f.)**
- **0.1.b Vault auth lifecycle (H1)** — AppRole + token, renew loop, fail-closed on loss.
  *Effort* M. *Deps* 0.1.a. *Risk* Med (timing). *Rollback* config back to token.
  *Validation* Vault contract test (login, renewal, `vault:vN:` signing, failover→503).
  *DoD* contract test green against live Vault. **(Code written; awaits 0.1.f.)**
- **0.1.c Resilience (H2)** — backoff+jitter, timeout, breaker, metrics.
  *Effort* S. *Deps* none. *Risk* Low. *Rollback* revert. *Validation* fault-injection unit tests.
  *DoD* breaker open/half-open/closed transitions covered. **(Code written; awaits 0.1.f.)**
- **0.1.d Atomic dev keystore (H4)** — temp+fsync+rename, checksum, per-key lock.
  *Effort* S. *Deps* none. *Risk* Low. *Rollback* revert.
  *Validation* corruption-detection + crash-simulation tests. *DoD* `KeystoreCorrupt` on tamper.
  **(Code written; awaits 0.1.f.)**
- **0.1.e Cross-provider strict resolution (M1)** — fail-closed `provider_for`; verification
  provider set; decommission runbook. *Effort* S. *Deps* 0.1.a. *Risk* Low.
  *Rollback* revert. *Validation* unit: unknown-provider key_id ⇒ `ProviderNotAvailable`.
  *DoD* no default fallback path remains. **(NEW — not yet coded.)**
- **0.1.f Execution validation (FINAL gate)** — install deps; run ruff + mypy + import-linter;
  full pytest; Vault contract test against a real Vault; pin RFC-8032 Ed25519 known-answer
  vectors; run the benchmark regression suite. *Effort* M. *Deps* 0.1.a–e.
  *Risk* Med (first real execution). *Rollback* n/a (validation only).
  *Validation* all gates green. *DoD* **only on green does ADR-0009 become FINAL.**
- **0.1.g Hybrid fail-closed guard** — reject `hybrid=true` with no PQC-capable provider at
  startup. *Effort* XS. *Deps* 0.1.a, D5. *Risk* Low. *Rollback* revert.
  *Validation* unit: unsatisfiable required-set raises at sign. *DoD* no silent single-sig path.

PQC provider onboarding and the dual-sign ceremony are **out of Wave 0.1** (future wave, triggered
by NIST-track provider availability); the architecture already accommodates them with no change.

## Consequences

- One auditable trust boundary; private keys never in DB or long-lived app memory ⇒ the
  insider/DBA threat model is addressable. Provider abstraction ⇒ Vault→HSM→cloud-KMS by config
  over a 20-year life with zero consumer impact and preserved historical verifiability.
- A hard operational dependency on Vault/HSM (availability, rotation, DR of key material) and
  per-call network latency (mitigated by public-key caching, batching, and the breaker).
- The self-authenticating envelope makes every signature self-describing and downgrade-proof,
  which is the property court admissibility and 20-year crypto-agility both require.

## Security Review Board — Verdict

**APPROVED (architecture) — no remaining Critical findings.** All prior Critical (C1) and High
(H1–H4) findings are resolved in the design; M1 and M6 are resolved. High/Medium items are tracked
as roadmap waves 0.1.e/0.1.f/0.1.g. The implementation reaches **FINAL** only on Wave 0.1.f green.
This ADR must not be weakened without a new adversarial audit.
