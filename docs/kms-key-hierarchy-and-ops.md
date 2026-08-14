# KMS — Key Hierarchy & Operational Runbook (ADR-0009)

The KMS is the single, provider-agnostic cryptographic trust boundary (`platform.crypto`).
Consumers use the `KeyManagementService` facade with a logical `KeyRef(purpose, name)`; they
never name an algorithm or a provider. Provider selection is entirely by configuration.

## Key hierarchy

```
Root Trust Key (root_trust)          apex — signs/attests the roots below; offline-guarded (HSM)
        │
        ├── Evidence Root (evidence_root)   ADR-0003: custody/audit entry signatures + Merkle roots
        ├── Event Root    (event_root)      ADR-0007: signed outbox envelopes
        ├── Storage Root  (storage_root)    ADR-0008: object/column envelope encryption (KEK)
        └── Session Root  (session_root)    ADR-0010: token/secret keying
                    │
                    └── Derived Keys        per-object/column data keys via generate_data_key()
```

- **Signing roots** (`root_trust`, `evidence_root`, `event_root`) use the policy signing
  algorithm (default **Ed25519**; ECDSA-P256 or a PQC scheme by config).
- **Encryption roots** (`storage_root`, `session_root`) use **AES-256-GCM** and mint **data keys**
  by envelope encryption; the plaintext DEK is used and discarded, only the wrapped DEK is stored.
- Every signature carries `algorithm, provider, key_id, key_version, signature_version,
  created_at` so historical evidence stays verifiable across rotation and algorithm migration.

## Providers (config: `KMS_PROVIDER`)
| Value | Status | Notes |
|---|---|---|
| `dev` | implemented | pyca/cryptography software crypto; **forbidden in production** (fails closed) |
| `vault_transit` | implemented | Vault Transit; keys never leave Vault |
| `aws_kms` / `azure_key_vault` / `gcp_kms` / `pkcs11` | registered slots | add behind `CryptoProvider` before selecting |

## Provisioning runbook
1. **Choose provider** via `KMS_PROVIDER` (prod: `vault_transit` or an HSM/cloud provider; never `dev`).
2. **Vault:** enable Transit (`vault secrets enable transit`); grant the app policy `create/read/
   sign/verify/encrypt/decrypt/datakey/rotate` on `transit/*`; deliver the token via Vault + the
   External Secrets Operator (never an env var set by hand).
3. **Create the hierarchy** (once, via `scripts/kms_bootstrap.py` or the facade):
   `create_key(KeyRef(ROOT_TRUST))`, then `EVIDENCE_ROOT`, `EVENT_ROOT`, `STORAGE_ROOT`,
   `SESSION_ROOT`.
4. **Rotation:** `kms.rotate(ref)` mints a new version; old versions are retained for verification.
   Rotation is an audited lifecycle event.
5. **Health:** the HTTP `/readyz` probe and the KMS metrics expose provider health
   (`ready`/`degraded`/`unavailable`); startup **fails closed in production** if the KMS is
   unavailable.

## Operational guarantees
- **Fail closed:** a signing subsystem that cannot reach its keys refuses to serve (no unsigned
  evidence). Enforced at HTTP startup and surfaced by `/readyz`.
- **No key material** ever appears in logs, exceptions, tracebacks, serialization, or tests;
  secret-bearing value objects redact their repr; fixtures use ephemeral generated keys.
- **Observability:** every operation emits latency + provider + algorithm + purpose + result via
  Prometheus + structured logs (see `platform/crypto/metrics.py`).
- **Resilience:** provider calls use bounded jittered-backoff retry behind a per-provider circuit
  breaker with a per-call timeout (`platform/crypto/resilience.py`); breaker state and retries are
  metered. Vault auth supports token and **AppRole** with **automatic lease renewal** and graceful
  re-authentication; on lease loss the provider goes `unavailable` (fail closed).

## Key durability & disaster recovery (ADR-0009 H3)

**Why this is existential:** losing an evidence/audit signing key makes **all** evidence ever
signed under it **permanently unverifiable**. Key durability is therefore a first-class
evidentiary requirement, not an ops afterthought.

**Design (per provider):**
- **Backup.** Production keys live only in the backend (Vault/HSM/cloud KMS); durability is the
  backend's responsibility: Vault with Integrated Storage (Raft) + Vault's own snapshot/DR
  replication; cloud KMS with the provider's multi-region durability; HSM with a documented
  key-backup ceremony to a second HSM. **The application never holds exportable private keys**, so
  there is no app-side backup to leak.
- **Disaster recovery.** Vault DR-replication secondary in a second site/region; documented
  promotion runbook; DR is exercised in staging before it is trusted (aligns with
  `deployment-architecture.md`).
- **Escrow / recovery (root of trust).** The `root_trust` key uses an **M-of-N key ceremony**
  (Shamir split of the unseal/recovery material) with shares held by distinct custodians in
  separate custody; recovery requires a quorum. Documented, witnessed, and logged.
- **Rotation retention.** Old key **versions are retained forever for verification** (never
  destroyed) so historical evidence stays verifiable after rotation; only *new signing* moves to
  the latest version. `destroy` is reserved for provably-unused keys and leaves an audited
  tombstone (never used on a key that has signed evidence).
- **Recovery procedures.** Runbook covers: Vault seal/unseal, DR promotion, AppRole credential
  rotation, breaker-open triage, and a **chain re-verification** pass (ADR-0003) after any
  recovery to prove no evidence was lost or altered.
- **Key ceremony.** Root and per-domain roots are generated in a witnessed ceremony (HSM-backed
  in production), recorded with attestation where the provider supports it (`capabilities.
  attestation`).

> Status: this is the **architectural design** required by the audit (H3). The Vault/HSM DR
> wiring and the ceremony scripts are provisioned per deployment; the application-side contract
> (never export private keys, retain versions, fail closed) is implemented in `platform.crypto`.
