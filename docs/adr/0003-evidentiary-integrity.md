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
| §3 External anchoring — **Merkle + WORM** | 1.3 | **Built and running** — `platform/crypto/{merkle,anchoring}.py`, `platform.ledger_anchors`, COMPLIANCE-mode Object Lock. Anchors are **produced** by a 4-hourly cutter (`modules/ingestion/anchor_jobs.py`) and enforced at boot by a WORM readiness probe (`platform/storage/worm.py`). Truncation and rollback detected end to end, against a live database and a live MinIO Object Lock bucket |
| §3 External anchoring — **RFC-3161 timestamp** | 1.3c | **Built** — `platform/crypto/tsa.py`: DER `TimeStampReq`, full CMS `SignedData` verification (digest, nonce, signed attributes, signer signature, timestamping EKU, validity-at-genTime, chain to configured anchors). Tokens are requested over each Merkle root, persisted in `tsa_token_ref` and in the WORM document, and re-verified by the Verification Engine. Disabled by default; **refused outright in air-gapped/classified profiles** |
| §6 Verification Engine — **online report** | 1.4 | **Built** — `platform/crypto/verification.py` (pure, three-layer, three-state) behind `GET /api/v1/evidence/{id}/verify`; api-design.md §5.1. Proven against real tampering on a live database in `test_verification_db.py` |
| §6 Verification Engine — **scheduled re-verification** | 1.4 | **Built** — `modules/ingestion/integrity_jobs.py`, an hourly arq cron job over the audit ledger and the most recently active custody chains. Alarms via Prometheus metrics + a `CRITICAL` log line; **not** via the notification module (see the amendment below) |

**Every part of this ADR is now built.** As of Wave 1.4 the guarantees are not merely constructed but *checked* — an endpoint reports on any chain on demand, and a scheduled job re-verifies both ledgers and alarms on a break. Wave 1.3c closes the last residual, backdating, with RFC 3161 timestamping. The one remaining limitation is deliberate and documented below: air-gapped deployments cannot reach a TSA at all, so they retain the backdating residual by design, and no deployment gets revocation checking.

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

## Amendment (2026-09-25) — §6's "alarms on any break" is a metric, not a notification

§6(b) requires the scheduled job to "alarm on any break" without saying through what. Implemented as
a Prometheus metric an Alertmanager rule fires on, plus a `CRITICAL` structured log line —
`deployment-architecture.md`'s committed Prometheus/Grafana/Loki stack, and the two signals an
on-call operator actually receives.

**Deliberately not the `notification` module,** and the reason is worth recording so it is not
"fixed" later by someone who assumes it was an omission. Every dispatch path in that module requires
an explicit `recipient_user_id` carried on the triggering event, and `notification/events.py` states
outright that a handler "cannot invent someone to notify." A ledger integrity failure has no user in
its domain — it is addressed to security operations. There is no by-role user lookup anywhere in the
codebase and `NotificationRule` resolution is unbuilt, so a notification handler for this would
resolve zero recipients on every firing: code that looks like alerting while reaching nobody, which
is strictly worse than no alerting at all.

When recipient resolution exists (a by-role lookup, or ADR-0010's `case_members`), the job is where
it gets wired in, and that will need an `integrity.verification_failed` event added to
`event-driven-architecture.md` §25's catalog. No such event type is published today.

**One scope limit to record.** The audit ledger's *entry-level* checks run over a bounded window of
the most recent entries (default 5,000), because that ledger is global and grows without limit; its
*anchor* checks run over the whole chain, because an anchor exists precisely to catch a deleted tail
and a deleted tail is by definition not inside a window of surviving rows. Custody chains are
verified in full — they are bounded by how many times one item was touched. The scheduled sweep
covers the most recently active custody chains (default 250) rather than every chain ever written;
any chain can be verified in full on demand through the API.

## Amendment (2026-09-25b) — anchors are cut on a schedule, and the bucket is checked at boot

IC-029 shipped anchoring as "a library plus a store, not a running job", and IC-030 shipped a verifier
that checks whatever anchors exist. In a deployment where nothing cut a batch, that was **none** — so
every piece worked and together they proved nothing. Two operational components close that.

**The cutter** (`modules/ingestion/anchor_jobs.py`, 4-hourly) reads each ledger in its canonical global
order, finds the contiguous tail no anchor covers, and publishes one anchor over it. Three properties
are load-bearing:

- **Anchors are contiguous, forward-only intervals.** An anchor commits to a range of one ordered
  list, so scattered gaps cannot be anchored — the boundary only ever moves forward.
- **A watermark (15 minutes) holds back the newest entries.** Ordering is by `occurred_at` and clocks
  are not monotonic; without the lag, a write whose clock ran behind could be committed after a cut
  but sort before its boundary, changing the recomputed root for a ledger nobody touched. That is a
  false tampering alarm on healthy data, which is how a real alarm gets ignored.
- **An unresolvable boundary refuses the cut.** If an existing anchor's `last_entry_hash` is no longer
  in the chain, the ledger has been truncated. The cutter must not fall back to "nothing is anchored"
  and publish a fresh commitment over the survivors: that would be a valid, correctly-signed anchor
  attesting to the attacker's version of events — laundering a truncation into proof rather than
  failing to detect it. The run stops and leaves the failing anchor on record for the verifier.

**Custody anchors are cut over a global order, not per evidence item**, and this follows from §5's
schema rather than from preference. `platform.ledger_anchors` records a range by first/last entry hash
with no column scoping it to a subject. Per-item anchors would therefore be unlocatable by any other
item's verification, and each would be reported as a missing range — a false failure on every evidence
item as soon as a second one was anchored. One global order (`occurred_at, custody_event_id`, the
tiebreak being required for a reproducible Merkle root) makes every anchor locatable by every verifier
with no schema change. The cost is that verifying one custody chain reads the whole ledger's hash
column; scoping anchors by subject would remove that, and is a schema change worth its own increment.

**The readiness probe** (`platform/storage/worm.py`) runs in both entrypoints' startup. `ensure_worm_bucket`
deliberately does not verify an *existing* bucket, because Object Lock is fixed at creation and cannot
be repaired — which makes a pre-existing, correctly-named, non-WORM bucket the dangerous case: creation
is skipped, writes may succeed, and every anchor in it is deletable with nothing in the data to reveal
it. The probe refuses a bucket without Object Lock, and refuses one whose default retention is
GOVERNANCE (bypassable by `s3:BypassGovernanceRetention` — exactly the insider being defended against).
A bucket with no default rule passes, because `put_immutable` names COMPLIANCE on every write.

It **fails closed in production** and, outside production, logs and gates `/startupz` so a degraded
start is never silent — the same posture as the existing KMS and bucket-bootstrap checks, which keeps
local development runnable without a WORM-capable MinIO.

## Amendment (2026-09-25c) — RFC 3161 timestamping, and the `asn1crypto` dependency

§3's second half is built. This records the dependency decision CLAUDE.md requires, and the two
limitations that a reader must not have to discover from the code.

### Dependency: `asn1crypto`, used only as a codec

`cryptography` (already a dependency) cannot verify CMS `SignedData` — its `pkcs7` module signs,
decrypts, and loads certificates, but exposes no verification entry point, and it has no TSP support
at all. So a codec was needed. The candidates:

| Option | Verdict |
|---|---|
| **`asn1crypto`** | **Chosen.** Pure Python, no build toolchain and no native extension, so it installs identically into an air-gapped mirror. Ships complete `tsp` (TimeStampReq/Resp, TSTInfo) and `cms` (ContentInfo/SignedData) definitions. Widely deployed as the ASN.1 layer beneath `oscrypto`/`certvalidator` |
| `rfc3161ng` | Rejected. Higher-level and would have written less code, but it pulls `pyopenssl` (whose maintainers direct new work to `cryptography`) and is thinly maintained for something on an evidentiary path |
| `pyasn1` + hand-written TSP/CMS schemas | Rejected. Hand-writing ASN.1 definitions for a security boundary is exactly the "lighter version of a security control" `security-architecture.md` warns against |
| Hand-rolled DER | Rejected outright, for the same reason IC-029 deferred this work in the first place |

**The split is the point:** `asn1crypto` parses and encodes DER and does nothing else. Every signature
check and every certificate parse goes through `cryptography`. No signature verification is
hand-rolled.

One quirk worth recording because it produces a misleading error: `asn1crypto.tsp.TimeStampResp`
declares `timeStampToken` **required**, while RFC 3161 §2.4.2 makes it OPTIONAL. A real rejection
carries status only, so parsing one through asn1crypto's class raises — and the useful diagnostic
(*why* the TSA refused) would surface as "malformed response", sending an operator to debug the wrong
thing. `platform/crypto/tsa.py` defines a lenient structure for exactly this.

### What the verifier checks

Response status; CMS content type is `signed_data` wrapping `tst_info` with content present (not
detached); the timestamped digest is *our* digest under the algorithm the token names; the nonce
matches what was sent; exactly one `SignerInfo`; the `content-type` and `message-digest` signed
attributes bind the signature to *this* TSTInfo; the signature over the DER `SignedAttrs` verifies;
the signer certificate carries `id-kp-timeStamping` as its **only** EKU, marked critical (§2.3); the
certificate was valid at `genTime`; and it chains to a configured trust anchor. SHA-1 imprints are
refused — a timestamp over a collidable digest attests to nothing even when the token verifies.

### Limitation 1: no revocation checking

No CRL fetch, no OCSP. Both require network calls that an air-gapped deployment cannot make and that
would turn verification of *archived* evidence into a live-connectivity problem. The consequence is
real: a token signed by a certificate revoked after issue still verifies. The mitigation is
operational — the trust anchor set is the deployment's control surface, so a TSA that should no
longer be trusted is removed from configuration — and every anchor still carries the WORM and Merkle
guarantees, which do not depend on the TSA at all. Path building is likewise bounded: the signer is
verified against the trust anchors directly, or through at most the intermediates the token carried.

### Limitation 2: air-gapped deployments keep the backdating residual, by design

Timestamping is **off by default**, and an air-gapped or classified profile that sets
`TSA_ENABLED=true` **fails to start** — checked for every profile, not only production, because a
developer running the air-gapped profile is usually doing so to prove the absence of egress, and a
silently-ignored TSA URL would make that exercise worthless. This is `deployment-architecture.md`'s
zero-egress rule applied literally: RFC 3161 requires reaching a third party, and an air-gapped
deployment must have no path to one.

Those deployments therefore keep the narrow residual §3 describes: an attacker holding both the
application and the clock could publish a fresh anchor over doctored history and claim it is old.
They still cannot replace an anchor already in WORM. The honest description of an air-gapped
deployment is *"tamper-evident and non-repudiable against modification and removal; not proof against
backdating"* — which is what this ADR said of every deployment before 1.3c.

### Timestamping fails OPEN, and it is the only thing in `publish` that does

An unsigned anchor or an unwritten WORM object is a lie, so both abort the cut. An untimestamped
anchor is a weaker *true* statement — it still proves non-truncation. Aborting a cut because a third
party's TSA was unreachable would let someone else's outage stop this platform committing to its own
evidence, trading the guarantee we control for the one we do not. The absence is always recorded,
never inferred: `tsa_token_ref` stays NULL and the Verification Engine reports the anchor as
untimestamped.

**A missing token is not a finding and does not degrade a verdict.** Every anchor cut before 1.3c and
every anchor in every air-gapped deployment carries none; degrading those to `partial` would flip
correct ledgers to a hedged verdict permanently and drain the meaning out of the one state that is
supposed to mean "this cannot be proven". It is reported as `AnchorFinding.timestamped` and a
report-level `untimestamped_anchors` count. A token that is *present and invalid* is a
`tsa_token_invalid` **failure** — downgrading a forgery to "no timestamp" would mean an attacker could
attach junk and lose nothing.

### Storage

`tsa_token_ref` holds base64 of the DER token, not a pointer. The token is a few hundred bytes, and a
reference to something stored elsewhere would be one more thing that can go missing between an anchor
and its proof of time. Base64 because the column is `Text` (§5) and changing an evidentiary table's
column type is a migration this does not need. The same token is also written into the WORM anchor
document, so an auditor holding only the object and the public keys can answer "when was this
committed?" without the database — the database being the thing under suspicion.

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
