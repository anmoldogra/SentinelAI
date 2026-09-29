# SentinelAI — Implementation Log

Chronological record of **implementation-level** corrections and notable choices made while building
to the frozen architecture. This is deliberately **not** an architecture log — architecturally
significant decisions are ADRs (`docs/adr/`). Entries here record fixes, reversals, and
implementation choices so they are traceable without re-reading git history. Newest last.

---

## 2026-07-28 — IC-001: Removed duplicate exception taxonomy (`platform/errors.py`)

**Type:** Implementation correction (not an architectural change).

**Problem.** An earlier Wave-1 step created `sentinelai/platform/errors.py` (a `SentinelError`
hierarchy). This **duplicated** the canonical domain-exception hierarchy already defined in
`sentinelai/shared/exceptions.py` — which is mandated by the Backend Implementation Guide (Part 11,
higher precedence than the Wave implementation notes) and already used by 38 files, with the HTTP
error-envelope mapping already wired in `entrypoints/http/exception_handlers.py`.

**Resolution.**
- `sentinelai.shared.exceptions` is the **single canonical** exception hierarchy. No second taxonomy.
- Removed `src/sentinelai/platform/errors.py` and its duplicate-only test `tests/unit/test_errors.py`.
- Reverted `config.ConfigurationError` to a plain startup/infrastructure `Exception` — it is a
  fail-closed *startup* error, not an HTTP domain exception, so it does not belong in the domain
  hierarchy.
- Added `tests/unit/test_shared_exceptions.py` guarding the canonical `code` ↔ `http_status`
  contract (api-design.md §2.4) against the real hierarchy.

**Docs touched.** `implementation-wave-1.md` §2/§9/§20 corrected to mark the error-handling component
DONE and point at the canonical location. **No ADR changed.**

**Lesson.** Grep for an existing implementation (`shared/`, `platform/`, module `*/exceptions.py`)
before building any "new" platform component; the guide, not the Wave notes, is authoritative on
placement.

---

## 2026-08-14 — IC-002: Object-storage foundation completion (error taxonomy, `exists`, ingestion wiring)

**Type:** Implementation choices (not an architectural change). ADR-0008 §1 only; §2–6 untouched.

**Context.** `platform/storage/` (port + MinIO adapter + factory + contract/unit/integration tests)
already existed. This increment closed the gaps that kept it from being production-wired.

**Choices made.**

1. **`ObjectNotFound` subclasses both `StorageError` and `KeyError`.** The port had documented
   missing-object lookups as raising `KeyError`, but a bare `KeyError` is not a meaningful platform
   exception and `botocore.ClientError` was leaking through the port for every other failure. The
   new `storage/exceptions.py` taxonomy (mirroring `platform/crypto/exceptions.py`) fixes both;
   the dual inheritance keeps the previously documented `KeyError` behaviour working rather than
   silently changing a contract other code may rely on.
2. **Presigned uploads go to `settings.storage_bucket`, not a `quarantine` bucket.** ADR-0008 §2
   requires presigned PUT into a quarantine bucket that is never served, with promotion into the
   evidence bucket after scanning. Quarantine/scan/promote is explicitly a later increment, so
   `reserve_upload` presigns into the single configured bucket today. **This is a known deviation
   from ADR-0008 §2 and must be revisited by the quarantine increment** — it is not a design
   decision to keep.
3. **Object key convention is `evidence/{category}/{artifact_type}/{evidence_id}`, addressed as
   `s3://{bucket}/{key}`.** The `s3://` form matches the CEM `payload_ref` example (§14). The key is
   deterministic because `POST /evidence/uploads` deliberately stores nothing — the client must be
   able to name the same object in the later `POST /evidence` finalize call, and the reservation
   response schema (`evidence_id`, `upload_url`) is fixed by api-design.md.
4. **`verify_integrity` stays `NotImplementedError`.** It needs ADR-0008 §3's server-side hashing
   over stored bytes; its message now names that increment instead of the stale "storage not built".

**Docs touched.** This log only. No ADR, no API contract, no schema change.

**Still open from W1-07/W1-11/W1-12.** Scratch-bucket bootstrap and the `/readyz` object-store check
were not built; `EvidenceService` is wired through the composition root, but no startup
`ensure_bucket` runs. `case_management`'s report-download path still carries its own
storage-deferred markers.

---

## 2026-08-18 — IC-003: ADR-0008 §3 `integrity_verification_status` transition conflicts with ADR-0004

**Type:** Verified architectural conflict — ~~unresolved~~ **RESOLVED by ADR-0015** (2026-08-18,
same day). See the IC-004 entry below for the resolution and its implementation.

**What was built.** Quarantine placement (ADR-0008 §2) and server-side streaming integrity
verification (ADR-0008 §3 / ADR-0003 §4): `platform/security/digest.py` digests an
`AsyncIterator[bytes]` without buffering, and `EvidenceService.verify_integrity` streams the stored
object through it, compares constant-time against the recorded hash, and appends an
`integrity_reverified` custody event (an event type CEM §4 already defines) carrying the
**recomputed** digest — on success *and* on mismatch, so a failure is auditable. A mismatch then
raises `IntegrityVerificationFailedError`; nothing is ever marked verified.

**The conflict.** ADR-0008 §3 and ADR-0003 §4 both require
`evidence.integrity_verification_status` to **transition** to `verified`/`failed`. That is an
`UPDATE` on `ingestion.evidence`. ADR-0004 installs a `BEFORE UPDATE OR DELETE ... RAISE` trigger on
exactly that table (`202607280002_ingestion_append_only.py`, generated by
`platform/db/append_only.py`) with **no column-level carve-out**. The two requirements cannot both
hold: the status field can never advance from its INSERT-time value while the trigger exists.

**Why it was not resolved here.** Every available resolution is architecturally significant and
needs an ADR, not a silent code choice:
- weaken/except the append-only trigger → downgrades an evidentiary guarantee (forbidden);
- add an append-only `evidence_integrity_verifications` table and derive the status → a new table
  is not in `database-design.md`, and CLAUDE.md forbids inventing one without documenting it;
- hash synchronously during `ingest_evidence` so the row is INSERTed already `verified` → matches
  ADR-0008 §3's "record the server hash in the custody `ingested` event", but streams a multi-GB
  forensic image inside an HTTP request, which ADR-0008's own context rules out.

**Consequence today.** `integrity_verification_status` stays `pending` for payload-bearing evidence.
The authoritative record of a verification is the custody ledger entry, which is append-only and
therefore consistent with ADR-0004.

**Pre-existing instances of the same conflict (NOT introduced here, NOT fixed here).**
`EvidenceService` already mutates `ingestion.evidence` rows in three places that the same trigger
would reject against a real Postgres: `legal_hold` (two sites) and `status = "superseded"`. Unit
tests use in-memory fakes and the append-only integration test skips without Postgres, so this has
never been exercised. It needs its own remediation task.

---

## 2026-08-18 — IC-004: IC-003 resolved — derived state transitions (ADR-0015)

**Type:** Architectural resolution + implementation. **ADR-0015** (`docs/adr/0015-append-only-state-transitions.md`).

**Decision.** Evidence operational state (`status`, `legal_hold`,
`integrity_verification_status`) is **derived at read time from append-only records**; the
columns keep INSERT-time genesis values and are never UPDATEd. Derivations: `status` from the
`supersedes_evidence_id` replacement linkage (CEM §12); `legal_hold` from the latest
`legal_hold_applied`/`legal_hold_released` custody event (exactly ADR-0004 §4's prescription);
`integrity_verification_status` from the latest `integrity_reverified` event's recomputed digest
compared constant-time against the recorded hash. The service applies the derived values to ORM
instances via `set_committed_value` (never dirties, never emits UPDATE).

**Why not the alternatives.** A trigger carve-out downgrades the ADR-0004 guarantee on exactly
the most legally consequential fields. A new `evidence_state_transitions` table duplicates the
custody ledger — two append-only records of the same fact that can disagree. Synchronous hashing
addresses only one of the three fields and violates ADR-0008's own streaming constraint.

**Changed.** `EvidenceService` (three in-place mutation sites removed; `_overlay_derived_state`
on get/list; derived already-superseded and legal-hold-gate checks), `EvidenceRepository`
(`has_replacement`, derived `status` list filter via EXISTS subquery),
`CustodyEventRepository.last_of_types`, `database-design.md` §3.2 derived-state rule,
`tests/unit/test_evidence_state_derivation.py` (9 tests incl. a dirty-instance regression guard).

**No migration.** The resolution requires no schema change — deliberately: the append-only
trigger and privileges installed by 202607280002/202607300002 stay byte-identical, and the
`status` enum note in database-design.md §3.2 already lists `superseded` as a value (the column
simply never reaches it physically; the derivation supplies it).

**Still open.** The `pending_validation`/`quarantined`/`tombstoned` states listed in
database-design.md §3.2's status enum have no writer yet (pre-existing). List-filter derivation
for `status` is pushed to SQL, but `legal_hold`/`integrity_verification_status` are not list
filters today; if they become filters they need the same treatment.

---

## 2026-08-18 — IC-005: Scan + promotion workflow (ADR-0008 §2, security §25)

**Type:** Implementation choices. No ADR change; no schema change; no API change.

**Built.** `platform/security/scanner.py` (`MalwareScanner` port, `ScanResult`,
`DummyMalwareScanner`, `build_malware_scanner`); `ObjectStorage.copy_object` (server-side, with
ranged `UploadPartCopy` above S3's 5 GiB single-copy limit so multi-GB forensic images promote
without transiting the process); `EvidenceService.scan_and_promote`; the
`scan_uploaded_evidence` arq job wired to worker context.

**Choices made.**

1. **`storage_evidence_bucket` NOT added — `storage_bucket` already is it** (default
   `sentinelai-evidence`; the comment beside `storage_quarantine_bucket` already names it as the
   promotion target). Adding a second setting would have been a duplicate for one bucket.
2. **Custody event types reused, none invented.** CEM §4's `event_type` enum is closed and the
   event-driven catalog §25.2 lists only three ingestion events, so: promotion appends
   **`transferred`** (custody location moves from quarantine to the evidence store) and a blocked
   scan appends **`analyzed`**. No new custody type, no new domain event. Derivation keys on the
   event **type**, never on note text (ADR-0015's rule).
3. **`payload_ref` joins the derived fields (ADR-0015).** Promotion moves the object but the
   genesis `payload_ref` names quarantine and cannot be UPDATEd, so current location is derived:
   a `transferred` event ⇒ evidence bucket, same key. This keeps `get_download_url` and
   `verify_integrity` correct after promotion with no schema change.
4. **Production refuses to start with `MALWARE_SCANNER_PROVIDER=dummy`** (`validate_for_profile`,
   mirroring the `kms_provider=dev` guard). **Consequence to be aware of:** since no real engine
   adapter exists yet, a production-grade profile currently cannot start at all. That is
   deliberate fail-closed behaviour — a no-op scanner would satisfy §25's gate while scanning
   nothing — and it makes the missing adapter impossible to ship past.

**Known deviation from §25.** §25 says a forensic detection is recorded "as metadata (added to
the evidence's `tags`/`attributes`, per CEM §2)". Those columns are on the append-only evidence
row and cannot be written after INSERT (ADR-0004/ADR-0015), so the detection is recorded on the
custody ledger and in `platform.audit_log` instead. Same auditable fact, different location than
§25's wording; §25 should be reconciled with ADR-0015 when next revised.

**Not done.** No `evidence.scanned`/`evidence.promoted` domain event (would need a §25.2 catalog
entry first). §25's "notify the uploading analyst" on a block is unimplemented — it needs the
notification module. Blocked items do not derive `status = quarantined` (that enum value still
has no writer, as noted in IC-004).

---

## 2026-08-18 — IC-006: ClamAV adapter + `evidence.scanned` event

**Type:** Implementation choices. No ADR change; no schema change; no API change.

**Built.** `platform/security/clamav.py` (`ClamAVMalwareScanner`), `ScannerHealth` +
`MalwareScanner.health()`, the `clamav` branch of `build_malware_scanner`, clamd settings, and
`evidence.scanned` (constant, outbox publish, and event-driven-architecture.md §25.2 catalog row).

**Choices made.**

1. **No clamd client dependency.** The protocol needed is two commands (`INSTREAM`, `VERSION`),
   so the adapter speaks it directly over `asyncio` streams. This avoids taking a transitive
   supply-chain surface (§42-43) on a third-party wrapper for ~100 lines of framing, and keeps
   `pyproject.toml` unchanged.
2. **Early-abort is a normal outcome, not an error.** clamd replies and closes the socket as soon
   as it matches a signature (or hits `StreamMaxLength`), which breaks the in-flight write.
   `ConnectionResetError`/`BrokenPipeError` during upload are caught and the verdict is read from
   the socket — otherwise every detection on a large file would surface as a transport failure.
3. **Any non-`OK`/non-`FOUND` reply raises `ScannerNotAvailable`.** An `ERROR` reply (e.g. size
   limit exceeded) must never degrade into a clean verdict.
4. **`ScannerHealth` reuses `platform.crypto`'s `HealthState`** rather than defining a parallel
   enum. `DEGRADED` = engine answers but signatures are older than
   `CLAMAV_MAX_SIGNATURE_AGE_HOURS`; an unparseable `VERSION` reply is also `DEGRADED`, never
   `READY` — a daemon that will not state its signature version cannot be graded fresh.
5. **UNIX-socket transport is resolved dynamically** (`getattr(asyncio, "open_unix_connection")`)
   so the module imports and type-checks on Windows, where a configured socket path yields a
   clear `ScannerNotAvailable` instead of an `AttributeError`.
6. **`evidence.scanned` publishes on every outcome** — clean, blocked, and forensic-exception —
   per §25's "every scan result, clean or not, is logged". Outbox insert sits in the same
   transaction as the custody write (guide Part 6). Thin payload (§18): identifiers, verdict,
   and disposition; no evidence content. A failed scan publishes nothing.

**Not done.** No `/readyz` wiring for `scanner.health()` (the readiness probe still checks
postgres/redis/kms only). No notification consumer — the catalog row records `notification` as the
intended consumer, but the module is a later increment. No freshclam/offline signature import
(§41): the adapter observes freshness, deployment supplies it.

**Production startup is now possible again** with `MALWARE_SCANNER_PROVIDER=clamav` — the
IC-005 deadlock (production rejects `dummy`, no other adapter existed) is resolved.

---

## 2026-08-18 — IC-007: Health probes + bucket bootstrap (W1-07, W1-11 complete)

**Type:** Operational wiring. No new abstraction; no schema, API-contract, or business-logic change.

**Built.** `/readyz` gained object-store and malware-scanner checks; a `/startupz` startup gate;
an async-safe TTL cache over every probe result; and `ensure_bucket` bootstrap for the quarantine
and evidence buckets in the HTTP lifespan.

**Choices made.**

1. **Object-store probe is `exists()` on a key that should not exist**, not `ensure_bucket`.
   The wave-1 spec says "HeadBucket", and `ensure_bucket` *creates* — a health check with a side
   effect is the wrong shape. `exists` is a HEAD: reachable ⇒ `False`, unreachable or missing
   bucket ⇒ raises (`BucketNotFound`), which the probe reports as `unreachable`. No port change
   was needed and nothing is written.
2. **DEGRADED scanner passes readiness; DEGRADED KMS still fails.** The KMS is on the HTTP
   request path, so only READY passes (pre-existing behaviour, unchanged). The scanner runs in
   the *worker*, off the HTTP path, so §25 stale signatures are surfaced in the body and logged
   without draining HTTP traffic. UNAVAILABLE fails either way. The asymmetry is deliberate and
   encoded in `_SCANNER_PASSING`.
3. **Bucket bootstrap fails closed in production only**, mirroring the existing KMS posture:
   outside production the process still serves, but `/startupz` keeps reporting 503 so a degraded
   start is never silent.
4. **`/startupz` reports whether initialization ever *succeeded*, not current reachability.**
   That is what distinguishes it from `/readyz`: a pod that came up without buckets keeps failing
   the startup probe even while its dependencies are momentarily reachable.
5. **TTL cache is per-check with one lock per key** (3 s). The lock collapses a burst of
   concurrent probes into a single downstream call — the stampede it exists to prevent — and
   successes and failures expire on the identical TTL, so a recovered dependency surfaces within
   one window.

**Not done.** The worker entrypoint has no probe endpoints (it is not an HTTP process); migration
currency is not part of the startup gate (wave-1 §9 lists "migrations-current" as a startup-probe
input — it needs a schema-version check that does not exist yet).

---

## 2026-08-18 — IC-008: Migration currency gate + round-trip reversibility (W1-11, W1-16)

**Type:** Operational wiring + one dependency correction. No schema, API, or business-logic change.

**Built.** `platform/migrations/currency.py` (`check_migrations_current`, `MigrationStatus`);
`migrations_current` wired into the `/startupz` gate; `tests/integration/test_migrations.py`
(upgrade-head → downgrade-base round trip); `make test-migrations`.

**Findings and choices.**

1. **No `pass`-only downgrade exists.** An AST sweep of all 13 migrations across the 9 module
   histories found every `downgrade()` already fully implemented (1–16 statements each). The
   "replace any `pass`" task had nothing to do; instead two AST tests now *enforce* the property
   so a future migration cannot regress it (CLAUDE.md rule 9).
2. **`psycopg` was undeclared but required.** `migrations/env.py` rewrites the app's `+asyncpg`
   URL to `+psycopg`, so **no migration could run on a clean install** — `make migrate` would have
   failed. Added `psycopg[binary]>=3.1` to `pyproject.toml`. This is a dependency addition beyond
   the increment's literal scope, made because the round-trip deliverable is unrunnable without
   it, anywhere, including CI.
3. **Currency check is read-only and cwd-independent.** It resolves script directories from the
   package location rather than `alembic.ini`, and compares each schema's `alembic_version` row
   against that module's script head. It never stamps, creates, or applies — applying stays the
   ArgoCD PreSync job's job (deployment-architecture Part 5).
4. **A stale schema is never fatal at startup.** It logs an error and fails `/startupz` only.
   During a rolling deploy a new pod can legitimately start moments before the PreSync migration
   job finishes; holding traffic via the startup probe is correct, killing the process is not.
5. **The round trip uses a throwaway database, not schemas in the dev one**, so a half-applied
   run can never leave a developer's database broken. It also asserts no module table survives
   `downgrade base`, which is what actually catches a wrong downgrade.

**Regression caught during verification.** The new test's *skip* path took 260 s: psycopg retries
a dead endpoint for minutes, where asyncpg (used by the other integration tests) fails fast. An
explicit `connect_timeout=3` on the reachability probe restored the suite to ~66 s. Any future
sync-driver probe needs the same explicit timeout.

**Still open.** The round trip has never actually executed — no Postgres is reachable in this
environment, so it skips honestly. W1-16 is complete as code but unproven until CI provisions a
database; that is the one thing standing between this and a real reversibility guarantee.

---

## 2026-08-18 — IC-009: CI pipeline, service containers, pre-commit, PR template (W1-15)

**Type:** CI/governance wiring. No application code, no schema, no test-logic change.

**Built.** `.github/workflows/ci.yml` (the §17 pipeline as 9 gates + an aggregating `ci-passed`
check), `.pre-commit-config.yaml`, `.secrets.baseline`, an extended PR template, workflows README,
and Makefile targets that keep local commands identical to CI.

**Findings and choices.**

1. **§17's marker-based selection does not work in this repository.** §17 specifies
   `pytest -m "unit or architecture"` and `pytest -m integration`, but no test carries a pytest
   marker and no markers are registered — `-m integration` would collect **zero** tests and report
   a green build. CI selects by **path** (`tests/unit`, `tests/integration`) instead, matching how
   the suites are actually organised. Marking the suites so §17's literal wording works is a
   separate change to the test files, deliberately not made here.
2. **`tests/architecture/` does not exist**, so the architecture gate is `lint-imports` (both
   contracts) — which is the real boundary enforcement — rather than a pytest selection.
3. **Coverage floor is 73%, not §17's 90%.** Measured platform coverage is 73.8%. Setting 90 would
   make every PR red on day one, so the gate is a **ratchet** that prevents regression and must be
   raised. The gap is real and is not closed by this increment. (The displayed "74%" is rounded —
   `--cov-fail-under=74` fails; verified before committing to 73.)
4. **MinIO and Vault use `docker run`, not `services:`.** Both need command arguments
   (`server /data`, dev-mode listener) and a GitHub Actions service container cannot be given a
   command. Postgres and Redis remain `services:` with health checks.
5. **CI fails if an integration test SKIPS.** These tests skip themselves when their dependency is
   unreachable — correct locally, dangerous in CI, where a skip is indistinguishable from a pass.
   A service that fails to start now fails the build.
6. **`scripts/migrate.sh` is committed mode 100644**, so `./scripts/migrate.sh` would fail with
   "Permission denied" on a fresh checkout. CI invokes it as `bash scripts/migrate.sh`.
7. **The PR template was extended, not duplicated.** `.github/PULL_REQUEST_TEMPLATE.md` already
   existed; adding `pull_request_template.md` would have created two templates GitHub resolves
   ambiguously (and the same file on a case-insensitive filesystem).
8. **No image signing.** deployment-architecture Part 5 requires cosign-signed images from an
   approved base; that infrastructure is not bootstrapped. CI builds and CVE-scans the image but
   does not push or sign — a signed-looking artifact backed by no real key would be worse than
   none.

**Unproven.** The workflow has never executed — GitHub Actions cannot run locally. YAML parses and
the pre-commit config passes `pre-commit validate-config`, but "the 7 skipped integration tests
pass in CI" is a claim the first real PR run has to settle, not something verified here.

---

## 2026-08-18 — IC-010: platform coverage raised to the §17 Tier-0 floor (90%)

**Type:** Test coverage. **No application code changed.**

**Result.** `sentinelai.platform` coverage 73.8% → **93.83%** from the unit suite alone; the
CI gate and `Makefile` floor are now a hard **90**, no longer a ratchet.

**What was newly covered.**

1. **`crypto/backends/vault.py` 23% → 93%** — the largest single gap. Driven against an
   in-process fake Vault via `httpx.MockTransport`: only the transport is substituted, so real
   request construction, header/namespace handling, and status-code translation are exercised.
   Covers token vs AppRole auth, lease-renewal failure marking the provider UNAVAILABLE
   (fail-closed, H1), health-code mapping, the 404→`KeyNotFound` / 5xx→`KmsUnavailable`
   retry boundary, and the full sign/verify/encrypt/decrypt/datakey surface.
2. **`crypto/resilience.py` 35% → ~100%** — breaker state machine (closed→open→half-open→closed),
   bounded retry, deterministic errors never retried, mid-retry breaker trip, and an enforced
   per-call timeout. Jitter and the clock are pinned; no test sleeps.
3. **`events/dispatcher.py` 35% → 99%**, plus inbox/outbox/UoW — at-least-once delivery, per-handler
   transactions, one failing handler not blocking others, requeue vs dead-letter, graceful
   shutdown, and "one bad poll cycle never kills the dispatcher".
4. **`crypto/kms.py` 71% → high** — registry routing/dedup, aggregate health (worst provider wins),
   auditable key lifecycle against a real `DevKmsProvider`, and `build_provider`'s fail-closed
   production guards (placeholder Vault token, AppRole without credentials, unsupported auth).
5. **`storage/minio.py` 86% → high** — `copy_object`'s ranged `UploadPartCopy` path above the
   5 GiB single-copy limit, byte-for-byte reassembly, and abort-on-failure.

**Coverage config.** Added `[tool.coverage.run] omit` for `*/migrations/env.py` and
`*/migrations/versions/*`. These are executed by Alembic's own runtime — `env.py` runs
module-level side effects against a live migration context and revisions are proven end-to-end by
the `upgrade head -> downgrade base` round trip — so measuring them as library code reports
coverage the unit suite structurally cannot provide. This is the only denominator change; it is
documented in `pyproject.toml` rather than left implicit.

**Two test-authoring bugs caught during the work** (both mine, both in tests): patching
`asyncio.sleep` globally stopped the polling loop from yielding, so a background task never ran —
the stub now sleeps zero *and* yields; and two assumed APIs were wrong
(`AlgorithmPolicy.from_config` is keyword-only; there is no `KeyPurpose.EVIDENCE_ENCRYPTION` —
the storage purpose is `STORAGE_ROOT`).

---

## 2026-08-19 — IC-011: Case becomes a rich aggregate (partial ADR-0011 conformance)

**Type:** Targeted conformance change. No schema, migration, API, or event change.

**Context.** The case_management module was already fully implemented (models, repo, service,
12 router endpoints, `case.created`/`case.status_changed` outbox events, 15 tests) — a re-issued
build brief for the module was resolved as: implement ONLY the genuinely missing portion, which
was ADR-0011 §1's requirement that the **`Case` aggregate itself** owns the
`open→closed→archived` machine. It previously lived in the service (`_TRANSITIONS` +
inline mutation), i.e. anemic-model shape.

**Change.** The vocabulary (`STATUS_*`, `VALID_STATUSES`, `TRANSITIONS`) and the machine moved to
`models.py`; `Case` gained `transition_to()` (validates, mutates `status`/`closed_at` as one
invariant, returns the previous status) plus intention-revealing `close()`/`reopen()`/`archive()`.
The service's `_apply_transition` now delegates to the aggregate and keeps only orchestration
(history row, outbox publish, audit). Behaviour and raised exception types are identical;
`tests/unit/test_case_aggregate.py` (11 tests) proves the machine at the aggregate surface,
including that a refused transition leaves state untouched.

**Deliberately NOT done.**
- ADR-0011 for `Evidence` and `Finding` — same pattern, separate increments; ADR-0011 stays
  Proposed until all three aggregates conform.
- ADR-0005 (UoW commit at the entrypoint, "services never commit") — the re-issued brief asked
  for it, and ADR-0005 agrees, but every module currently commits in services; conformance is a
  cross-cutting pass touching all modules/routers/jobs/tests, not something to smuggle into a
  case-only change. **Open conformance gap, now explicitly on the record.**
- ADR-0011 §3 (aggregates raise domain events, application maps to outbox) — the service still
  publishes integration events directly; part of the full ADR-0011 pass.

---

## 2026-08-19 — IC-012: ADR-0005 conformance — transaction ownership moved to entrypoints

**Type:** Cross-cutting conformance pass. No domain logic, schema, event, or API-contract change.

**Change.** All 23 service-level `commit()` calls removed (case_management 8, ingestion 11,
investigation 4); commits added at the entrypoints: 6 case endpoints, 8 ingestion endpoints,
4 investigation endpoints, and the `scan_uploaded_evidence` job wrapper. Routers obtain the SAME
request-scoped UoW instance via FastAPI's per-request dependency cache
(`Depends(get_<module>_uow)` in both the service factory and the endpoint). Enforcement:
`tests/architecture/test_transaction_boundaries.py` AST-scans every module `service.py` and fails
on any `.commit()`/`.rollback()` call site.

**Semantics deliberately preserved (the two commit-before-raise sites).** A rejected
`POST /evidence` persists its intake record + `evidence.validation_failed` outbox event, and a
failed `verify-integrity` persists the MISMATCH custody entry — both endpoints catch the domain
error, commit, and re-raise. Rolling those back would erase exactly the records the failure
exists to create. `POST /evidence/batch` runs as ONE transaction: per-item failures are pre-flush
domain checks (never DB errors), so failed items' intake records ride the single commit and the
207 body stays accurate; ADR-0005 §4's per-item savepoints are unnecessary because there is no
per-item commit to isolate anymore.

**Latent bug surfaced and resolved by the convention.** `InvestigationService.create_relationship`
NEVER committed — under the old convention `POST`-created relationships would have silently
persisted nothing against a real DB (fakes hid it; there is also no HTTP route for it today, it
is the Phase-3 correlation job's API). Under entrypoint ownership its future caller's wrapper
commits, closing the hole structurally.

**Tests.** 4 assertions inverted from `commits == 1` to `commits == 0` (services must NOT
commit); `test_verification_commits_within_the_existing_uow` renamed/inverted accordingly;
+2 architecture tests. Integration (router-level) tests unchanged — they now exercise the router
commit path.

**ADR-0005 status: implemented.** The remaining §4 nuance (explicit savepoints where per-item
independence is intentionally required) has no live use case after this pass.

---

## 2026-08-19 — IC-013: notification consumes evidence.scanned (security §25 analyst alert)

**Type:** Feature slice — the consumer side of `evidence.scanned`. No migration (the notification
schema, including `inbox_events`, already existed).

**Built.** `platform/notifications/` (`NotificationSender` port, `NotificationMessage`,
`LoggingNotificationSender`, `build_notification_sender`, `NOTIFICATION_SENDER_PROVIDER`);
`NotificationService.dispatch_for_evidence_scanned`; the `on_evidence_scanned` handler +
registration in `notification/events.py`; `NotificationRepository.add`/`exists_for_source` and
`DeliveryRepository.add`.

**Producer change (necessary, not incidental).** `Notification.recipient_user_id` is NOT NULL but
`evidence.scanned` carried no recipient — the consumer could not name who to notify. Added
`collector_user_id` to the payload, mirroring `evidence.ingested`, which already carries it, and
consistent with §18's thin-event rule (carry what the common-case consumer needs). §25.2's catalog
row and the payload-contract test were updated in the same change; the test caught the change,
which is what it exists for.

**Two-layer idempotency.**
1. **Inbox claim** on `(event_id, "notification.on_evidence_scanned")` — insert-first, before any
   side effect; a redelivery short-circuits.
2. **Business key** `(recipient_user_id, source_module='ingestion', source_reference_id=evidence_id)`
   via `exists_for_source` — stops a *different* event (a re-scan) re-sending a message the analyst
   already has. This is the §25.9 catalog's documented key; without it the Inbox alone would let a
   second scan of the same evidence duplicate the alert.

**Notify rule.** `not is_clean and not promoted`. A clean scan is normal; a forensic-category
detection is promoted deliberately (§25's carve-out — malware in a disk image IS the evidence), so
neither notifies. Both are still consumed and `mark_processed`, so an ignored event is not
redelivered forever.

**Channel failure does not discard the notification.** The in-app row is the durable Phase-1
delivery, so a sender exception is caught, recorded as a `failed` delivery row, and published as
`notification.delivery_failed` — rather than raised, which would roll back the handler transaction
(including the inbox claim and the row) and re-notify on retry.

**One deliberate deviation:** `events.py` imports `NotificationService` *inside* the handler, not
at module scope, because `service.py` imports this module's published-event constants — a
top-level import would close an events↔service cycle. Commented at the import site.

**Still stubbed in this module (out of scope, unchanged):** the three other consumed-event
handlers (`correlation_generated`, `case_status_changed`, `case_report_generated`), the
router-facing service methods (inbox list, mark-read, redeliver, rule CRUD), and their repository
reads. No real SMTP/Slack adapter.

---

## 2026-08-20 — IC-014: notification inbox read/update path

**Type:** Feature slice. No migration, no schema change, no API-contract change.

**Built.** `NotificationRepository.get_by_id` + `list_for_recipient` (keyset, newest-first);
`NotificationService.list_notifications` + `mark_read`; router wired to the real pagination
values and an ADR-0005 commit on the PATCH.

**Choices made.**

1. **The repository takes a DECODED cursor**, not the raw string its stub signature declared
   (`cursor_created_at` / `cursor_notification_id`). Opaque-cursor codec is application logic and
   lives in the service in every other list path in this codebase (`case_management`,
   `ingestion`); matching that beat matching an unimplemented stub's signature.
2. **`(created_at, notification_id)` tuple comparison, both DESC**, fetching `limit + 1`. The id
   tie-break is load-bearing, not decoration: notifications raised in one transaction share a
   timestamp, and a `created_at`-only cursor would either skip or loop on them. Covered by a test
   that pages through five same-timestamp rows.
3. **Recipient scoping is in SQL**, not applied after the fetch — a caller cannot reach another
   analyst's inbox regardless of service behaviour. The recipient is always
   `actor.user_id`, never a parameter.
4. **`mark_read` raises `ForbiddenError` (403), not 404**, for someone else's notification —
   api-design.md §8's explicit, reasoned exception to the NOT_FOUND-hides-existence convention.
   Following the doc over the prompt's "e.g. NotFoundError or ForbiddenError".
5. **Idempotent by preserving the first timestamp**: a second `mark_read` returns the
   notification unchanged rather than rewriting `read_at`, so when the analyst first saw an alert
   stays true.
6. **Router previously hardcoded `next_cursor=None, has_more=False`** — that was a lie once
   listing worked, so `list_notifications` returns `(items, next_cursor, has_more)` matching every
   other list service, and the router now reports real values.

**Still stubbed in this module (out of scope, unchanged):** `redeliver`, rule management
(`list_rules`/`create_rule`/`update_rule`) and their repository reads, and the three other
consumed-event handlers.

---

## 2026-08-20 — IC-015: Postgres-backed keyset-pagination proof for the notification inbox

**Type:** Test-only increment. Zero source changes — the Postgres run required no repository fix.

**Built.** `tests/integration/test_notification_db.py`: two tests driving the REAL
`NotificationRepository.list_for_recipient` against a real Postgres — (1) an 8-row inbox with a
3-row timestamp tie plus a bystander's rows, paged at limit 3, asserting exactly-once retrieval,
strict `(created_at, notification_id) DESC` global order, SQL-level recipient scoping, and page
shape 3+3+2; (2) five rows on ONE identical timestamp paged at limit 2, so every cursor boundary
falls inside the tie — the case where a `created_at`-only cursor would repeat or drop rows.

**Setup choice: throwaway database, not a throwaway schema or dev-schema reuse.** Three reasons:
the ORM models are pinned to the `notification` schema (a throwaway schema can't host them without
model surgery); the CI **integration job deliberately does not apply migrations** (only the
separate migration-round-trip job does), so the migrated schema cannot be assumed to exist there;
and a dev database must never be seeded with test rows. The test creates
`sentinelai_notiftest_<hex>`, builds exactly the production table definitions
(`Base.metadata.create_all` limited to `notification_rules` + `notifications` — the former rides
along for the FK), and drops the database in `finally` (`WITH (FORCE)`, Postgres ≥13; CI runs 16).

**Skip path bounded** (`connect_args={"timeout": 3}` on the reachability probe) per IC-008's
lesson — a keyless local run skips in seconds, and CI's "no integration test may skip" assertion
forces both tests to actually execute there.

**Local status: skipped honestly** (no Postgres reachable). The proof lands on the first CI run
with the Postgres service container; until then the row-value-comparison SQL remains
executed-in-CI-only, not executed-nowhere.

---

## 2026-08-20 — IC-016: the three remaining notification consumers

**Type:** Feature slice completing the module's event-consumer path. No migration, no new endpoint.

**Built.** `on_correlation_generated`, `on_case_status_changed`, `on_case_report_generated` in
`notification/events.py`, and their three `dispatch_for_*` service methods. All four consumed
events (with `evidence.scanned`) now have live handlers registered Critical-fast.

**Refactor inside the module (not unrelated):** the persistence/delivery/outbox tail that
`dispatch_for_evidence_scanned` already contained was extracted to
`NotificationService._create_and_dispatch`, so all four dispatches share one dispatch core.
`dispatch_for_evidence_scanned` became a thin wrapper; its behaviour is unchanged and its existing
suite still passes untouched (bar the fake-signature fix below).

**Producer payload amendments (IC-013's precedent, catalog updated in the same change).**
- `case.status_changed` gains **`owning_user_id`** — case_management has the owner in hand; this
  path is LIVE end to end.
- `investigation.correlation_generated` gains **`recipient_user_id`**, supplied via a new optional
  `case_owner_user_id` parameter on `create_relationship`. A parameter, not a lookup: investigation
  must not reach into case_management on a write path, and the (deferred) correlation job already
  loads the case to select its evidence, so it has the owner. Omitted ⇒ event still publishes,
  consumer ignores it.
- `case.report_generated` gains **`requested_by_user_id`** in the catalog only — its producer is
  the still-deferred report job. The handler reads it and falls back to `owning_user_id`. No
  producer code was invented for a stub.
- §17's thin-event table row for `case.status_changed` updated to match.

**Business idempotency keys, exactly as §25.9 specifies.**
- correlation: `(recipient, 'investigation', relationship_id)`.
- report: `(recipient, 'case_management', report_id)` — keyed on the report, so a regenerated
  report is a new fact and does notify.
- status: `(recipient, case_id, new_status)` — the extra `new_status` discriminator is carried by
  matching the stored message, which is composed as a pure function of exactly `case_id` and
  `new_status` (no timestamp, no previous status). `exists_for_source` grew an optional `message`
  argument for this. A test pins the purity: two transitions to `closed` from *different*
  previous statuses dedupe to one notification.

**Missing-recipient policy:** consumed, ignored, `mark_processed`. A handler cannot invent someone
to notify, and dead-lettering an otherwise valid upstream fact is worse than sending nothing.

**Gate failure encountered and fixed at the cause:** widening `exists_for_source` broke 10 tests in
`test_notification_scan_consumer.py`, whose in-memory fake still had the old signature. The fake
was updated to mirror the real repository — no production behaviour changed.

**Still stubbed in this module:** `redeliver`, rule management (`list_rules`/`create_rule`/
`update_rule`), `NotificationRuleRepository`, and `DeliveryRepository.list_for_notification`.

---

## 2026-08-20 — IC-017: async case-report generation (schema conflict resolved)

**Type:** Feature slice + the schema decision that blocked it. One migration.

**The conflict, and how api-design.md settled it.** `case_reports` required `storage_ref` and
`generated_at` NOT NULL and had no status column, so no row could exist before the job finished —
contradicting the async job-state-row pattern (guide Part 12). api-design.md §7 already specified
the resolution and was followed verbatim: `POST /cases/{id}/reports` creates the row **immediately
in `queued` state**, and `GET /reports/{report_id}` polls `status` over
**`queued|running|completed|failed`**. That vocabulary is the doc's, not the brief's suggested
"pending" — the doc wins.

**Migration `202608200001_case_reports_job_state`.** Relaxes `storage_ref`/`generated_at` to NULL;
adds `status`, `requested_at`, `failure_reason`; indexes `(case_id, status)`. New NOT NULL columns
are added nullable → backfilled (`status='completed'`, `requested_at=COALESCE(generated_at, now())`
— pre-existing rows are by definition finished) → constrained, so it is safe on a populated table.
`downgrade()` is a real inverse: it drops the index/columns, restores both NOT NULLs, and first
DELETEs unfinished rows, which reference no object and cannot satisfy the restored constraints.

**Job.** `generate_case_report(ctx, case_id, report_id)` is a thin wrapper (the ingestion-jobs
precedent): it resolves session + storage from the worker ctx and delegates to
`CaseService.complete_report`, which renders the Phase-1 JSON document (case, evidence links,
status history), streams it through the `ObjectStorage` port to
`s3://{storage_bucket}/reports/{case_id}/{report_id}.json`, marks the row completed, and publishes
`case.report_generated` — the event `notification` already consumes (IC-016), carrying
`requested_by_user_id` as that consumer's recipient.

**Ordering is load-bearing:** the row is marked `completed` only *after* the upload returns, so a
crash mid-upload leaves it `running` for arq to retry rather than advertising a report that is not
there. Idempotent: an already-completed report returns untouched, so a redelivery neither
re-uploads nor re-publishes. On failure the job rolls back, then records `failed` + the reason in a
**separate transaction** so a poller learns why, and re-raises for arq.

**Consequential changes.** `CaseReportRead` gained `status`/`requested_at`/`failure_reason` and
made `storage_ref`/`generated_at` optional (a queued row cannot validate otherwise);
`get_report_download_url` now raises `ReportNotReadyError` (409) instead of returning a NULL ref;
`POST /cases/{id}/reports` returns `{report_id, status}` + a `Location` header per §7 (it returned
a job id, which the client cannot poll). `database-design.md` §3.4 updated in the same change.

**Not done:** PDF/external reporting (JSON is the Phase-1 document per scope), and *presigning*
the report download — `get_report_download_url` still returns the `s3://` reference rather than a
short-lived URL, the same gap the endpoint had before this increment.

---

## 2026-08-20 — IC-018: presigned report download + disclosure audit

**Type:** Feature slice completing api-design.md §7's download contract. No schema, no migration.

**Built.** `CaseService.get_report_download_url` now parses `storage_ref`, mints a 900 s presigned
GET URL through the `ObjectStorage` port, and writes a `case.report_downloaded` row to
`platform.audit_log` — replacing the raw `s3://` reference it used to hand back. The router
commits, because this GET now performs a deliberate write (ADR-0005).

**DI change (the increment's one structural move).** `CaseService.__init__` gained a
**required** keyword `storage: ObjectStorage`, injected by `get_case_service` via
`Depends(get_object_storage)` — mirroring `EvidenceService` exactly. Required, not
optional-with-default: an optional dependency that half the methods need hides a
misconfiguration until a request fails in production. Cost: 14 construction sites updated
(11 unit-test lines, 1 integration override, 2 in `jobs.py`, which already had `storage` in
scope from the worker context). `complete_report` keeps its explicit `storage` parameter
untouched — the worker builds its own per-process client and does not go through FastAPI DI, and
the brief ruled the async job out of scope.

**Ordering is the security property.** Presign → audit → return. If the audit write fails the
transaction rolls back and the caller gets the error, never the URL, so no un-audited disclosure
credential can escape. The audit row deliberately records the *intent to disclose* at the moment
the credential is minted — the only moment the platform can observe, since the fetch itself goes
straight to object storage. The URL is never logged or audited: it is a bearer credential
(ADR-0008 §6), and persisting it would store a live secret in the audit trail. Both are pinned by
tests.

**Test updated, not loosened.** `test_downloading_a_completed_report_returns_its_reference`
asserted the old raw-`s3://` behaviour this increment deliberately replaces; it now asserts the
presigned form. +11 tests covering the URL target/TTL, the audit entry's contents, the
no-URL-in-audit rule, the failed-audit-yields-no-URL ordering, no-commit-in-service, and that an
unfinished report is never audited as disclosed.

**Two self-inflicted gate failures, both fixed at the cause:** an unused unpacked variable
(RUF059), and a monkeypatch applied before test setup that itself audits — so the "audit fails"
double exploded during arrange rather than act.

---

## 2026-08-20 — IC-019: end-to-end API test for the report lifecycle

**Type:** Test-only increment. **Zero application-code changes.**

**Built.** `test_report_lifecycle_api_flow` in `tests/integration/test_case_api.py` walks the
whole contract over HTTP: `POST /cases/{id}/reports` (202 + `status: "queued"` + `Location`) →
poll via that Location (200, `queued`, NULL `storage_ref`/`generated_at`) → premature
`GET .../download` (409 `CONFLICT`) → simulated worker → poll (200, `completed`) → download (200,
presigned `http` URL, never `s3://`). Plus a 404 case for a report requested on an unknown case.

**Why this is worth a test.** IC-017 and IC-018 each proved their half in unit isolation; nothing
proved they agree. This is the only test where the 202's `Location` header, the polled `status`
field, and the download gate must line up with one another — it would catch a `Location` pointing
at a route that does not exist, or a download gate reading a status the poller never reports.

**Two harness adjustments (test-only, no production change).**
1. `get_task_queue` reads `app.state.task_queue`, populated by the HTTP lifespan — which
   `ASGITransport` does not run. Added a `_RecordingTaskQueue` override, which also lets the test
   assert the job is enqueued as `("generate_case_report", (case_id, report_id))`.
2. `_app_with_overrides` now optionally accepts a shared `FakeObjectStorage`. Previously every
   `get_case_service` call built a fresh one, so the object written by the simulated worker would
   have been invisible to the download request — the test would have passed for the wrong reason.
   Default behaviour is unchanged for the existing tests.

**How the worker was simulated.** By calling `CaseService.complete_report` directly — the exact
method `generate_case_report` delegates to — against the same UoW and the same object store the
API is using. Honest boundary, stated in the test's docstring: this covers the state transition a
client observes, NOT the job wrapper's own transaction/rollback/retry handling, which has its own
unit tests. Invoking the real job function was rejected because it constructs its own
`CaseManagementUnitOfWork` from a session factory, which the fake UoW cannot supply.

---

## 2026-08-20 — IC-020: CI pipeline fixes (roles, SAST, SBOM, container build)

**Type:** CI configuration only. No `src/`, no tests, no schema.

**1. `test_privileges_db.py` skipped in CI.** The test skips when the ADR-0004 role
`sentinel_append` is absent, and a fresh Postgres service container has no such role. Fixed by
applying **the repository's own** `infra/postgres/bootstrap/001_roles.sql` before pytest, rather
than re-typing `CREATE ROLE` in YAML — CI now provisions exactly what production does, so the two
cannot drift. The script is idempotent, database-name-agnostic (`current_database()`), and
self-contained (roles + attributes + membership + database-scoped grants; no table dependencies),
verified by reading it end to end. A follow-up `psql` query prints the provisioned roles so the
log shows what CI actually created.

**2. Security scan (SAST) — root cause: bandit exits 1 on findings, not a missing install.**
Verified locally: `bandit -r src -ll` reports **5 MEDIUM** issues — four B608 (DDL assembled from
module-constant identifiers in `db/privileges.py` and the report migration; never user input) and
one B104 (binding `0.0.0.0`, correct for a containerised service). The job was therefore
permanently red-but-ignored (`continue-on-error: true`). Restructured into two passes: a full
MEDIUM+ report (`--exit-zero`, uploaded as an artifact) and a **real gate on HIGH only**
(`-lll`), which exits 0 today and will fail on any new HIGH finding. `continue-on-error` removed,
since the job's status is now meaningful.

**3. Dependency scan & SBOM — root cause verified, not guessed:** `cyclonedx-py` v7 has no
`--outfile` flag; it rejects it with `unrecognized arguments` (reproduced locally). Changed to
`-o`. Ran the corrected command against this repo's venv: exit 0, valid CycloneDX **1.6**, 133
components. Added a validation step that parses the SBOM and fails if it is not a populated
CycloneDX document — an empty SBOM looks like provenance without being it.

**4. Container build.** Replaced the bare `docker build .` with
`docker/build-push-action@v6` naming `context: apps/server` and `file: apps/server/Dockerfile`
explicitly. The workflow-level `defaults.run.working-directory` applies to `run:` steps **only**,
never to actions, so the previous form depended on an implicit and easily-broken assumption.
Added Buildx + GHA layer caching and pinned `trivy-action` to `0.28.0` instead of `@master`
(an unpinned third-party action at HEAD is both a supply-chain and a reproducibility risk).

**Honesty note.** GitHub Actions cannot run locally. Causes (2) and (3) were **reproduced and
verified** on this machine; (1) is proven by reading the test's skip condition against the
bootstrap SQL. For (4) the 8-second failure was **not** reproduced — Docker is unavailable here —
so that change hardens the job against the plausible causes (implicit context resolution, an
unpinned action) rather than confirming a diagnosis. The first real pipeline run settles it.

---

## 2026-08-20 — IC-021: Alembic revision IDs shortened to fit VARCHAR(32); Trivy ref reverted

**Type:** CI fix. No schema DDL, no application code.

**Root cause.** Alembic's `alembic_version.version_num` is `VARCHAR(32)`; any revision id longer
than that fails on `upgrade`, which is what broke the migration round-trip job.

**Scope correction — five ids were over the limit, not one.** The reported failure named
`202607280001_platform_append_only` (actually 33 chars, not 35). An audit of every revision id in
the repository found four more that would have failed the moment the first was fixed:

| old id | len | new id | len |
|---|---|---|---|
| `202607300002_ingestion_evidentiary_privileges` | 45 | `202607300002_ingestion_privs` | 28 |
| `202607300001_platform_evidentiary_privileges` | 44 | `202607300001_platform_privs` | 27 |
| `202608200001_case_reports_job_state` | 35 | `202608200001_case_reports_job` | 29 |
| `202607280002_ingestion_append_only` | 34 | `202607280002_ingestion_append` | 29 |
| `202607280001_platform_append_only` | 33 | `202607280001_platform_append` | 28 |

Fixing only the named one would have moved the failure, not removed it. Longest id is now 29.

**Applied to** `revision`, `down_revision`, and the `Revision ID:` / `Revises:` docstring headers
(leaving those stale would make the files lie about their own identity). Verified by AST after
rewriting: 14 revisions, all ≤32; every `down_revision` resolves to a known revision; exactly
9 heads — one per module schema, as the per-module history model requires; no stale references to
the old ids anywhere in the repo. `test_migration_currency.py`'s existing one-head-per-schema
assertions pass unchanged, independently confirming the chains.

**Filenames deliberately unchanged.** Alembic resolves revisions by the `revision` variable, not
the filename, so the rename is complete as-is; renaming the five files is cosmetic churn outside
this increment. It does leave e.g. `202607280001_platform_append_only.py` declaring
`revision = "202607280001_platform_append"` — worth a tidy-up pass later.

**Trivy.** Reverted `aquasecurity/trivy-action@0.28.0` → `@master` as instructed, since the pinned
tag did not resolve. Recorded inline as a known tradeoff: an unpinned third-party action is a
supply-chain risk (governance §43), to be re-pinned once a verified release ref is confirmed.

---

## 2026-08-20 — IC-022: container-build fix + apps/web scaffolding (ADR-0016)

**Type:** CI fix + new frontend app. No backend source changes.

**Container build — the reported fix would have swapped one failure for another.** Root cause
found: `apps/server/.dockerignore` listed `README.md`, so it was absent from the build context
and `COPY pyproject.toml README.md ./` failed. But `pyproject.toml:10` declares
`readme = "README.md"`, so dropping it from the COPY (the proposed fix) breaks `pip install .`
at metadata generation instead — **verified empirically** by building a minimal hatchling package
with a declared-but-missing readme, which fails with "Encountered error while generating package
metadata". Fixed at the actual cause: un-ignored `README.md`, one line, no Dockerfile or
`pyproject.toml` change, package metadata intact.

**ADR-0016 written first, because the docs require it.** `frontend-architecture.md`'s header note
and §48 state the stack choice "should be recorded as an ADR before implementation begins", and
no frontend ADR existed. It records React + React Query (already fixed by the architecture doc)
and closes what §2 left open: **Vite** (static output, offline-installable, Rollup splitting for
§39–41), **React Router**, **Tailwind v4 as the implementation of the §19 token layer**, native
**`fetch`** over Axios, and TS `strict` + `noUncheckedIndexedAccess` +
`exactOptionalPropertyTypes` to mirror the backend's `mypy --strict`.

**Structure follows §3, not the conventional tree.** The brief proposed
`components/ pages/ api/ utils/`; §3 explicitly forbids those top-level grab-bags and mandates
feature folders mirroring `apps/server/modules/*`. Built as `app/` · `shared/` · `features/cases/`.

**Two doc rules encoded as tooling rather than prose.**
- `security-architecture.md` §35 / §9: the token store is in-memory only, and ESLint bans the
  `localStorage`/`sessionStorage` globals so a regression fails the build instead of relying on
  review.
- §19: only *semantic* tokens are exposed to Tailwind via `@theme` (`bg-surface`, never
  `bg-zinc-900`), so bypassing the token layer is visible.

**A lint finding worth recording.** `strictTypeChecked` flagged the `crypto.randomUUID` fallback
as dead code, because the DOM lib types it as always present. It is not dead: `randomUUID`
requires a **secure context**, and an air-gapped deployment on plain HTTP genuinely lacks it
(§2's profiles). Resolved by narrowing `globalThis` to an optional shape so the guard is honest
to the type system — not by suppressing the rule.

**Verified by execution:** `npm install` (195 packages, exit 0), `npm run check`
(format + lint + typecheck, exit 0), and a real `npm run build` — 91 modules, Tailwind compiled,
vendor chunks split as configured. Backend gates re-run unchanged: 509 passed, 9 skipped.

**Deliberately not built:** any feature surface (§23–29), the auth context/login flow (§6), the
theme switcher, error boundaries (§42), or a test runner — the Case Dashboard renders static
placeholder markup and no server data.

---

## 2026-08-20 — IC-023: apps/web wired into CI and the dev stack

**Type:** CI + local-dev integration. No application source touched.

**CI.** Added `frontend-checks` to `ci.yml`: checkout → `setup-node@v4` → `npm ci` →
`npm run check` → `npm run build`, running in parallel with the backend jobs (the two apps share
no toolchain, so neither should wait on the other). **Added to `ci-passed`'s `needs`** — a gate
that is not aggregated does not gate anything.

**Three details that would otherwise have broken it silently.**
1. The workflow sets `defaults.run.working-directory: apps/server` for *every* job, so the
   frontend job carries a job-level override to `apps/web`. Without it, `npm ci` would have run
   against the backend directory.
2. **Node 22, deliberately not 24.** npm 11 (shipped with Node 24) gates lifecycle scripts behind
   an `allow-scripts` approval, which esbuild's postinstall trips — observed locally in IC-022.
   Node 22 ships npm 10, so `npm ci` installs esbuild's platform binary with no flag or
   suppression needed.
3. `npm ci` rather than `npm install`: it fails when `package.json` and the lockfile disagree, so
   a dependency added without committing the lockfile is caught here instead of drifting.

**Dev stack.** Added a `web` service to `apps/server/docker-compose.dev.yml` (the file
`make compose-up` drives; the repo-root compose is datastores only). `node:22-bookworm-slim` to
match the bookworm images already in the stack, `../web:/app` bind mount for HMR, port 5173,
`npm ci && npm run dev -- --host 0.0.0.0`.

**Two of those settings are load-bearing, not incidental.**
- **An anonymous `/app/node_modules` volume.** Without it the bind mount shadows the container's
  modules with the *host's*, whose esbuild/rollup binaries are compiled for the developer's OS
  and cannot run on linux. This is the classic Node-in-Compose failure.
- **`VITE_API_PROXY_TARGET: http://api:8000`.** Inside the compose network `localhost` is the web
  container, not the API. IC-022 made the proxy target configurable for exactly this.

**Makefile.** No root Makefile exists, and `apps/server/Makefile` is backend-scoped — so rather
than adding Node targets there, `compose-up`'s help text was corrected (it now starts `web` too
and said otherwise) and a `compose-logs` target added for tailing a single service.

**Verified by execution, not just YAML parsing:** all three YAML files parse; a structural script
asserts the working-directory override, the ci-passed aggregation, the lockfile's existence, that
every npm script CI/compose invokes is defined, and that `../web` resolves to a real package. Then
the exact CI commands were run — `npm ci` (195 packages), `npm run check`, `npm run build` — all
exit 0. Backend suite unchanged at 509 passed.

**Not verified:** the compose `web` service has never been started (no Docker daemon available
here); it is reasoned from the file, not observed.

---

## 2026-08-21 — IC-024: Redis port remap + Case Dashboard wired to the API

**Type:** Compose fix + first real frontend feature. No backend source changed.

**Compose.** `redis` maps host **6380 -> container 6379** (6379 was already allocated on the
developer's host). Only the host side moves: `api`/`worker` reach Redis over the compose network
at `redis:6379`, so their `REDIS_URL` is untouched — verified by parsing the file back.

**Frontend.** `shared/api/pagination.ts` (keyset helpers: `withPageParams`, `nextCursor`,
`flattenPages`), `shared/api/errors.ts` (§12's code -> UI-treatment taxonomy),
`features/cases/{types,api/getCases,api/useCases}`, and the Case Dashboard rendering skeleton /
error / empty / table + "Load more".

**React Query.** `useInfiniteQuery`, not `useQuery`: the endpoint is keyset-paginated with no
total count, so "next page from this cursor" is the only shape it supports. `getNextPageParam`
returns `undefined` (not `null`) when exhausted — that is what React Query reads as
`hasNextPage === false` — and guards on `has_more` *and* a non-null cursor, because paging
forever on a missing cursor is a worse failure than stopping one page early.

**Dev auth seam — and the blocker it cannot remove.** `VITE_DEV_ACCESS_TOKEN` is read in
`token-store.ts` behind `import.meta.env.DEV`, so Vite dead-code-eliminates it from production
builds; **verified by grepping the built bundle — the variable name does not appear in `dist/`.**
The token still only lives in memory (§35).

It does **not** manufacture a session, and no mock token can. The backend resolves a bearer token
against a real `platform.sessions` row, and `SessionRepository.get_active_by_token` currently
raises `NotImplementedError` with no login endpoint anywhere — so **every authenticated endpoint
fails today regardless of what the client sends.** Against a live backend this page renders its
error state until the auth slice lands. The prompt's "mock JWT / mock authentication layer"
assumption does not match the implementation (there is no JWT and no JWKS); backend changes were
out of scope, so the seam is built to work the moment auth exists rather than faked.

> **Correction, added when this entry was committed (2026-09-08).** The paragraph above was
> accurate on 2026-08-21 but was overtaken before it landed: commit `70e6cfb` implemented
> `SessionRepository.get_active_by_token` and shipped `POST /api/v1/auth/login`. The seam is now
> usable rather than aspirational — `make dev-token` mints a token the server accepts. It is left
> in place because the login *screen* is still unbuilt; the sentence in `token-store.ts` that
> asserted the endpoint did not exist was corrected in the same commit. Nothing else in this entry
> changed.

**Two lint findings fixed at the cause.** `CaseStatus | string` collapses to `string`, so the
union bought nothing — `status` is now honestly `string`, while `STATUS_STYLES` is keyed
`Record<CaseStatus, string>` so adding a lifecycle state fails the build until it has a token.
Status colours were added as **semantic tokens** in `index.css` rather than hardcoded, per §19
(a first attempt double-inserted one block and missed another; corrected and verified as exactly
four theme blocks plus one `@theme` mapping).

**Notable: the Postgres-gated integration tests now execute.** With the dev stack up, the suite
went 509 passed/9 skipped -> **514 passed/4 skipped**. The migration round-trip, ADR-0004
privileges, append-only triggers, and the notification keyset-pagination SQL — all previously
"asserted in code, never executed anywhere" — now pass against a real Postgres, retroactively
confirming IC-008, IC-015 and IC-021.

---

## 2026-08-30 — IC-025: server-computed integrity on ingest (modernization Wave 1.5, ADR-0008 §3)

**Type:** Evidentiary-core correctness. Backend service + tests + ADR. No migration, no API shape
change, no new error code.

**The gap this closes.** For `payload_ref` evidence the server stored the **client-declared**
`integrity_hash` unchecked and left `integrity_verification_status` at `pending` forever. The
custody genesis entry therefore attested to what the submitter *said* it had uploaded, not to
bytes the server had seen. `verify_integrity` could recompute from storage, but nothing invoked
it at ingest, so the check was opt-in and after the fact. ADR-0008 Decision §3 and
`api-design.md` §167 both already specified the correct behaviour — this is an implementation
catching up to its own contract, not new design.

**What changed (`modules/ingestion/service.py`).**
- `_recompute_stored_digest(payload_ref, algorithm)` — the recompute block lifted out of
  `verify_integrity`. One primitive now shared by ingest-time verification and post-hoc
  re-verification, so the digest that *admits* evidence and the digest that later *re-checks* it
  cannot drift apart.
- `_verify_declared_payload(data)` — the policy layer. Streams the stored object, compares
  constant-time against the declared hash, returns the server digest. Three failure modes, all
  **422 VALIDATION_FAILED**: malformed `payload_ref`, no stored object, digest mismatch. The
  mismatch error carries *neither* digest — the declared one is the caller's own and the computed
  one is not disclosed for a submission being refused.
- `ingest_evidence` — verification runs **last**, only once every cheap rule has passed, so an
  already-invalid submission never pays for a full object stream. Its errors are merged into the
  same list every other rule uses, so a rejected upload produces exactly one `IntakeRecord` and
  one `evidence.validation_failed` event, identical to any other bad submission (§25.2). The
  genesis `ingested` custody entry now carries the **server** digest.
- The row inserts with `integrity_verification_status="verified"`, not `pending`. This is the
  INSERT's genesis value, never an UPDATE, so ADR-0004's append-only trigger and ADR-0015 both
  hold — and a later `integrity_reverified` event still wins at read time via
  `_overlay_derived_state`, which needed **no change** (verified, not assumed: it only overrides
  from `integrity_reverified` events). `pending` now means only "written before this rule existed".
- `supersede_evidence` — same helper. A replacement is payload-bearing evidence entering the
  store; without this, supersession would have been a documented way to write bytes the server
  never verified. Raises 422 directly rather than via an intake record, matching how `_validate`
  already rejects there.
- The `elif data.integrity_hash:` branch was **preserved deliberately**. Removing it would have
  silently changed behaviour for inline evidence carrying a declared hash — out of scope. Only the
  `payload_ref` branch moved.

**ADR-0008's §2/§3 tension, resolved and recorded.** §2 places server-side hashing in the
background scan job; §3 requires the server hash in the custody `ingested` event, which only
exists during `POST /evidence`. Both cannot hold with one hash computation. **Resolved in favour
of §3** — it is the stronger guarantee: hashing at ingest *rejects* a mismatch so no record is
ever created for unverified bytes, whereas the scan job could only mark an already-admitted row
`failed` after the fact. `api-design.md` §13's ingest sequence and its `POST /evidence` example
response (`verification_status: "verified"` on creation) already assumed this reading. ADR-0008
moved **Proposed → Accepted** with the tension written down rather than left to be rediscovered.

**The accepted cost, stated plainly.** The digest is computed **inside the HTTP request**. A
multi-gigabyte forensic image — the exact artifact class ADR-0008's own Context cites — is
streamed and hashed synchronously. This is tolerable at the file sizes the console handles today
and is **not** tolerable at disk-image scale. The fix belongs to the chunked/resumable-upload
increment: hash incrementally as parts arrive, so the digest is known before `POST /evidence` and
neither §2's single streaming pass nor §3's rejection guarantee is given up. No size-threshold
bypass exists **by design** — two ingest paths with different integrity guarantees would be worse
than one slow one. No dev "trust-client" flag was added either, despite
`modernization-roadmap.md` listing one as 1.5's rollback: rollback here is reverting the commit,
and a switch that weakens an integrity check is exactly what `CLAUDE.md` rule 8 warns against.

**Tests: 11 new, 9 reworked.** The three mismatch tests and the missing-object test previously
staged their failure by having a client lie at ingest — which ingest now rejects, so they could
never have reached `verify_integrity`. They now tamper with **storage after ingest**, which is the
scenario re-verification actually exists to catch, and assert against the tampered digest ("what
is on disk now"). Four download-URL tests in `test_ingestion_storage_paths.py` named objects that
were never uploaded and now store real bytes first;
`test_download_url_rejects_a_malformed_payload_ref` was kept rather than deleted, mutating the bad
value onto a stored row, because rows written before this rule can exist and the download path
must still fail closed on one. New coverage: server digest in the genesis entry, `verified` not
`pending`, mismatch → 422 with **no evidence row and no custody entry**, rejection still recorded
as a failed intake + `validation_failed` event, missing object → 422, malformed ref → 422,
large-object streaming, UoW not self-committed, inline evidence unaffected.

**Gates.** `ruff` (lint + format), `mypy --strict` (180 files), `import-linter` (2 contracts kept),
full suite **551 passed / 2 skipped** (up from 539), platform coverage floor **93.43%** against the
90% Tier-0 requirement.

**Coverage delta on `ingestion/service.py`: 84% → 85%** (269 statements/42 missed → 293/43).
+24 statements added with only +1 uncovered, so the new code is ~96% covered; module-wide
ingestion coverage moved 77% → 79%.

**Known gap, not covered.** The `ObjectNotFound` re-raise inside `_recompute_stored_digest` — the
object deleted *between* the `exists()` check and the read — is a genuine TOCTOU window that the
in-memory `FakeObjectStorage` cannot reproduce without a purpose-built hook. It was equally
uncovered before the extraction; the extraction did not introduce it, but it is now on a path that
runs for every payload-bearing ingest rather than only on explicit re-verification. Closing it
needs a storage fake that can fail mid-stream.

---

## 2026-09-08 — IC-026: RFC 8785 canonical encoding + crypto-agility columns (Wave 1.1, ADR-0003)

**Type:** Evidentiary-core foundation. New platform primitive + two additive migrations + docs.
No existing hash changed, no endpoint changed, no behaviour changed.

**What this increment is, and deliberately is not.** Wave 1.1 builds the *substrate* for
authenticated ledgers: a deterministic encoding, and the columns that let the format evolve. It
does **not** rewrite `_custody_entry_hash` or `_compute_hash` to use either — that is Wave 1.2
(complete signed preimage), and doing it here would have changed every entry hash in the same
change that introduced the encoding, leaving nothing stable to verify against. **The defects in
ADR-0003 Context §1/§2 remain live after this commit**; SR-4 is not closed.

**`platform/crypto/canonical.py`.** RFC 8785 JCS, pure stdlib, ~90 statements, 100% covered. The
three divergences from `json.dumps(sort_keys=True, separators=(",", ":"))` — the encoding both
ledgers use today — are each a silent hash change, and each is pinned by a test:

- **Numbers** follow ECMAScript `Number::toString`, not `repr(float)`: `1.0` renders `1`, `1e-7`
  renders `1e-7` (not `1e-07`). Implemented by taking the shortest round-tripping digits from
  `repr` and re-deriving only the *layout* per ECMA-262 §6.1.6.1.20, so it is exact without
  reimplementing shortest-float printing.
- **Non-ASCII** is emitted as literal UTF-8, not `é`.
- **Keys** sort by **UTF-16 code unit**, not code point. These differ only above U+FFFF — a
  surrogate pair leads with 0xD800-0xDBFF and so sorts below U+E000-U+FFFF. RFC 8785 §3.2.3's
  worked example is exactly this case (U+1F600 before U+FB33) and is asserted verbatim.

Everything unrepresentable **raises** rather than coercing: NaN/Infinity, non-string keys, lone
surrogates, non-JSON types, nesting past 100 levels. A silent coercion would change a preimage
without changing anything visible, which is the single failure mode the subsystem exists to
prevent.

**A real bound bug the database found.** The first draft rejected integers outside ±(2**53 - 1).
That is wrong, and only a live Postgres exposed it: `jsonb` stores numbers as `numeric` and
renders a stored `1e21` back as the digit string `1000000000000000000000`, which `json.loads`
yields as an **int**. The encoder accepted the value on the way in and rejected it on the way out
— precisely the round-trip divergence this module exists to prevent. The correct criterion is not
magnitude but exact representability: an int is admissible iff `float(n)` round-trips to `n`.
That admits 10**21 (= 5**21 * 2**21, and 5**21 fits in 53 bits) and 2**53 itself, while still
rejecting 2**53 + 1, which collapses onto the same double as 2**53. Had this shipped on the unit
suite alone, the failure would have surfaced years later as an unverifiable custody entry.

**Migrations.** `202609080001_platform_agility` (audit_log) and `202609080002_ingestion_agility`
(evidence_custody_events) add the six ADR-0003 §5 columns: `hash_algo`, `sig_alg`, `key_id`,
`preimage_version`, `signature` (BYTEA — no base64 layer to disagree about when verifying),
`anchor_ref`. Two migrations rather than one because each module owns its own schema and history
(database-design.md §5/§11); the change is one logical unit but must not cross a schema boundary.

All six are **nullable by design**. Wave 1.2 populates the signing metadata, Wave 1.3 fills
`anchor_ref` asynchronously. A null therefore means "written before that guarantee existed" — a
state the Verification Engine (1.4) dispatches on. NOT NULL with defaults would assert an entry
was signed under an algorithm when no signature exists, which is a worse lie on a court-facing
record than absent metadata.

**No privilege migration was needed, and that was checked rather than assumed.**
`platform.db.privileges` grants `INSERT, SELECT` at *table* level, not column level, so new
columns are covered by the existing grant; the ADR-0004 append-only trigger blocks row mutation
and is unaffected by DDL adding a column. Verified against a real database: both schemas at head,
all 12 columns present, correct types and nullability, existing rows untouched.

**ADR-0003 Proposed -> Accepted**, with the JCS-vs-CBOR ⟨OPEN⟩ resolved in favour of JCS: the
deciding criterion is *independent* verifiability, since a defence expert or oversight body must
recompute an entry hash with tooling we did not write, and JCS canonicalizes to ordinary JSON
text that any conforming implementation reproduces. CBOR is more compact but interposes a binary
decode between an auditor and the evidence, for a size saving irrelevant at ledger-entry scale.
The signature-algorithm and anchoring ⟨OPEN⟩s are **not** closed — they belong to Waves 1.2/1.3,
and anchoring in particular cannot be settled generically because an air-gapped deployment has no
reachable public TSA. The ADR now carries a verified per-decision implementation table so its
status cannot drift from the code again.

**Tests: 64 new** (58 unit, 6 integration). Specification vectors (RFC 8785 §3.2.3, twenty
ECMAScript number cases), determinism properties over a 1500-document seeded corpus, and six
JSONB round-trip tests against a real Postgres 16. The round-trip suite asserts **both**
directions — that Postgres really does reorder `jsonb` keys, and that canonicalization absorbs it
— so it cannot pass vacuously if `jsonb` semantics ever change.

`hypothesis` was deliberately **not** added. For a determinism property, a seeded corpus that is
byte-identical on every machine and in every CI run is worth more than random exploration, and it
leaves the air-gapped dependency surface unchanged. Worth revisiting if Wave 1.2's preimage work
wants shrinking on failure.

**Gates.** ruff (lint + format), `mypy --strict` (185 files), import-linter (2/2 contracts kept),
full suite **718 passed / 2 skipped** (up from 656 collected; the 2 skips are Vault and the KMS
benchmark, both opt-in via env var). Platform coverage **91.95%** against the 90% floor;
`canonical.py` at **100%**.

**Known gap, stated plainly.** The encoding is not yet *used* by anything. Until Wave 1.2 wires it
into both ledgers, `canonical.py` is a tested primitive with no production caller, and the
agility columns are six nulls per row. That is the intended end state of Wave 1.1 and the reason
1.2 must follow immediately rather than after unrelated work.

---

## 2026-09-08 — IC-027: complete ledger preimage + JCS wire-up (Wave 1.2, ADR-0003 §2)

**Type:** Evidentiary-core correctness. Both ledgers' hash functions rewritten, one new platform
primitive, the console's verifier rebuilt to match. The columns already existed (Wave 1.1); no new
schema.

**Scope, and the part of the wave's name this does not deliver.** Wave 1.2 is titled "complete
*signed* preimage". This increment delivers the **complete preimage** and the canonical encoding.
It does **not** sign: `signature`, `sig_alg` and `key_id` remain null and no KMS key is used by
either ledger. That belongs in the first paragraph rather than a footnote, because the two halves
close different threats and only one of them is now closed — see "What is and is not closed".

**`platform/crypto/ledger.py`** — one hashing primitive shared by both ledgers, because ADR-0003
treats custody and audit as a single integrity subsystem and the Wave 1.4 Verification Engine has
to re-derive either with the same code. Two copies of "canonicalize, then SHA-256" would drift,
and the drift would surface years later as an unverifiable chain. It carries
`LEDGER_PREIMAGE_VERSION = 1`, `LEDGER_HASH_ALGO = "SHA-256"`, a timestamp renderer, and a
`compute_entry_hash` that **injects the agility metadata into the preimage itself**. That is a
downgrade defense, the same reasoning `crypto.types.SignedHeader` already applies to
`required_algorithms`: metadata telling a verifier *how* to check an entry must be covered by what
it verifies, or an attacker rewrites the entry under the old partial format, resets
`preimage_version`, and it verifies under the weaker rules. A caller supplying either key is
rejected rather than silently overridden.

**Both preimages are now complete.** Audit went from 4 fields to 12, custody from 6 to 11. The
table-driven test reads the **live SQLAlchemy table definition** and fails if any column is
neither hashed nor on a five-entry exclusion list — `entry_hash` (the output) and
`signature`/`key_id`/`sig_alg`/`anchor_ref` (written after the hash exists). A prose claim of
completeness rots the moment someone adds a column; this fails the build instead. A second test
guards it in the other direction, so a renamed key cannot pass by matching nothing.

What was added is exactly what ADR-0003 Context §2 predicted: `occurred_at`, `actor_user_id`,
`actor_role`, `module`, `target_type`, `ip_address`, `user_agent` on audit; `actor_user_id`,
`actor_role`, `authority_ref`, `notes` on custody — **the attribution fields**. Who did it, in
what role, from what address, under what legal authority. On a chain-of-custody record those are
the evidence, and none of them were bound to a hash.

**Row identity is hashed too.** `audit_id` and `custody_event_id` are now generated in the service
rather than left to a column default, so they can enter the preimage. Without that, an entry's
contents could be transplanted onto a fresh id and still verify.

**Timestamps are hashed in the wire form.** The old preimage hashed Python's `isoformat()`
(`+00:00`); Pydantic serialises the same value with `Z`. So the string the server hashed was not
the string the client received, and the console carried a `toPythonIsoformat` helper whose own
docstring warned about millisecond truncation. Hashing the RFC 3339 `Z` form deletes that entire
class of defect — a client now hashes exactly the bytes it was given — and the helper is gone. A
naive datetime is rejected rather than assumed UTC, because guessing a timezone silently changes a
preimage.

**The console verifier had to change in the same commit, or every intact ledger would have been
reported as tampered.** `verifyChain.ts` mirrors the backend hash byte for byte and says so in its
own header. It now builds the preimage as a structure and encodes it with a new
`shared/crypto/canonicalJson.ts`, instead of a hand-written string template reproducing Python's
`sort_keys` order by eye. That TypeScript canonicalizer is about a hundred lines, and the reason is
worth recording: **JavaScript is the language JCS was specified against.** `String(n)` *is*
`Number::toString`; `JSON.stringify` *is* JCS's string escaping; `Array.sort()` on strings *is*
UTF-16 code-unit order. All three are the parts that took real work in Python.

`verifyChain.test.ts`'s fixtures are generated by running the actual Python implementation and
pasting its output verbatim — three entries covering null fields, accented text with an em dash, an
embedded quote and backslash, and three timestamp precisions. If the two canonicalizers ever
disagree, those hashes stop matching. That is a genuine cross-language conformance test rather than
the file restating its own logic.

**Mixed-format chains, and a distinction that matters legally.** An entry written before this
change carries `preimage_version = null` and **cannot** be re-derived. The verifier now dispatches
on the version and reports such an entry as *not independently verifiable*, degrading the overall
result to a new `partial` status — never `failed`. Calling an old-format entry "tampered" would be
a false accusation on a custody surface; calling it "verified" would claim a binding that was never
made. Neither is true, so neither is said. Chain linkage and sequence contiguity are still checked
for those entries, since neither depends on the preimage format. The same rule makes an older
client degrade rather than accuse when it meets a future version. `CustodyEventRead` gained
`hash_algo`/`preimage_version` so a client can dispatch at all; additive, so no API version bump
(`api-design.md` §2.2).

**One adjacent fix, made deliberately.** `ingest_evidence`'s inline-payload integrity hash still
used `json.dumps(sort_keys=True)` over a JSONB column — the same defect class, on an evidentiary
value, one line from two functions being fixed. It now uses `canonicalize`. Strictly outside Wave
1.2's brief; leaving one uncanonicalized evidentiary hash beside two fixed ones would just be a
defect with a longer fuse, since Wave 1.4 must recompute it from the stored row.

**What is and is not closed.** ADR-0003 Context §2 is closed: an attacker who edits a row is now
caught, including on the attribution fields. Context §1 is **not**: the chains are still unkeyed
and unanchored, so an attacker who can rewrite the whole chain and recompute every hash forward is
caught by nothing, because nothing yet requires a key they do not hold. **PRD SR-4 remains open.**
The honest description of these ledgers today is "tamper-evident against row-level edits", not
"tamper-proof against a privileged insider". ADR-0003's status table now says so per-decision
rather than per-wave.

**Tests: 49 new** (44 backend unit, 5 backend integration), plus the console suite rebuilt from 17
to 27. The integration tests write real rows to a throwaway Postgres, read them back, and
recompute — including one that performs the ADR-0003 threat model directly, `UPDATE`-ing
`actor_role` and `ip_address` on a stored audit row and asserting the recomputation exposes it
while the untouched `entry_hash` column does not. They also assert Postgres genuinely reorders a
`jsonb` object, so the round-trip guarantee cannot pass vacuously.

**Gates.** ruff (lint + format), `mypy --strict` (186 files), import-linter (2/2 kept), backend
suite **767 passed / 2 skipped** (up from 718; the 2 skips are Vault and the KMS benchmark, both
opt-in). Platform coverage **92.04%** against the 90% floor, with `crypto/ledger.py` and
`crypto/canonical.py` both at **100%**. Console: prettier, eslint, `tsc --build`, **27/27** vitest.

**Known gap.** No existing row was migrated, and none can be: the old hashes were computed over a
partial field set with a non-canonical encoder, so they are not recomputable by any amount of
backfill. The dev database's ten audit rows and one custody row will read as `partial` in the
console forever, which is the correct answer. Production is unaffected — there is no evidentiary
data, which is the whole reason ADR-0003 insisted this land first.

---

## 2026-09-08 — IC-028: KMS signatures on both evidentiary ledgers (ADR-0003 §1)

**Type:** Evidentiary-core security. Both ledger writers now sign; a new signing primitive, a key
provisioning path, and a KMS dependency threaded through five services. No schema change — Wave
1.1 already created the columns.

**What this closes, precisely.** Wave 1.2 made the entry hash cover every persisted field, which
catches an attacker who edits a row and leaves the digest stale. It could never catch the attacker
ADR-0003 is actually about: nothing in a hash requires a secret, so an administrator with database
access can edit a row, recompute its hash *and every subsequent hash*, and produce a chain that
verifies perfectly. Every entry is now also signed under `KeyPurpose.EVIDENCE_ROOT` with a key the
application's database role has no path to, so that forgery is detectable.
`tests/integration/test_ledger_signatures_db.py` carries out exactly that attack against a live
Postgres — raw `UPDATE`, hash recomputed the way the application would — and asserts the
verification failure. The property is demonstrated, not asserted in prose.

**`LedgerSigner` (platform/crypto/ledger.py).** Signing lives beside hashing deliberately: they
are two halves of one operation, the signature covers the hash, and Wave 1.4 must check both or
neither. A verifier that confirmed the hash and skipped the signature would report a forged chain
as intact.

**The signed message is canonical JSON, not a concatenation.** ADR-0003 §1 writes it as
`(sequence || prev_entry_hash || entry_hash)`. Implemented literally, that is ambiguous the moment
crypto agility does its job: a SHA-256 digest is 64 hex characters and a SHA-384 digest is 96, so
`prev || entry` stops being uniquely parseable as soon as two algorithms coexist — the classic way
to make two different tuples produce one signed byte string. It is built with JCS instead, which
costs nothing and keeps the signed bytes reproducible by an independent verifier. A **ledger
discriminator** is included so a signature made for the custody ledger cannot be presented as
valid for audit; there is a test for that. ADR-0003 §1 was updated to record the refinement rather
than leaving the code and the ADR disagreeing.

`sequence` is `null` for `audit_log`, which has no sequence column — its ordering is the chain
itself, and `entry_hash` already covers `audit_id`, so identity is bound regardless. A null
sequence is distinct from `0` in JCS, and that is tested.

**The `signature` column stores the whole envelope, not just the raw signature bytes.** The KMS
signs a `SignedHeader` (ADR-0009 C1), not the message directly, and that header carries a
timestamp captured at signing time and the required-algorithm set in force *then* — neither is
derivable afterwards. Storing 64 raw Ed25519 bytes would have produced signatures nobody could
ever verify again. The envelope is canonical JSON mirroring `SignedHeader.canonical_bytes()` field
for field, 366 bytes for a single Ed25519 signature, and versioned independently of
`preimage_version`. `sig_alg` and `key_id` are denormalized copies so operations can query "what
was signed under the key version we are retiring" — they are **not** verification inputs, and a
test rewrites both to lies and confirms verification is unaffected, because the envelope's own
copies are the ones the signature covers.

**`kms` is a required argument with no default, and that was the point.** Adding it to
`record_audit_event` broke seven call sites across five modules and the CLI; the type checker
found all of them. A default would have made an unsigned audit entry reachable by omission, which
is precisely the failure this increment exists to prevent. `EvidenceService`, `CaseService`,
`InvestigationService` and `AuthService` now take a KMS; the HTTP DI factories pull it from
`app.state` (the existing `get_kms` dependency, matching the `MfaRepository` precedent) and the
worker jobs reuse `ctx["kms"]`, which the worker entrypoint already built.

**Nothing provisioned keys before this, so `make ensure-keys` was needed.** No code path called
`kms.create_key`, and `create_key` is **not** idempotent — on an existing key it mints a new
version, i.e. it rotates. So the new CLI command checks existence first and creates only when the
key is genuinely absent. Silently rotating an evidence signing key because someone re-ran
`make create-admin` would be a serious operational fault. `create-admin` and `dev-token` now
ensure the key themselves, since both write audit entries.

**Measured cost (dev provider, local):** 1.17 ms per signature, 0.56 ms per verification. The full
backend suite went from 100 s to 137 s purely from real signing on every audited write.

### Two things that are worse than they look, stated plainly

**1. The KMS is now a hard availability dependency of every write path.** Signing happens inside
the caller's transaction and fails closed: if the KMS is unreachable, the audit write raises and
the whole business transaction aborts. For a legal record that is the correct trade — an unsigned
entry is a hole no later process can fill, because the bytes that should have been signed are gone
— but it means a Vault outage stops evidence ingestion, case updates, and logins, not just
auditing. It also puts a network round-trip inside a database transaction, holding locks for its
duration. ADR-0003's Consequences already anticipated this and named batched Merkle signing (Wave
1.3) as the mitigation.

**2. This increment widens a pre-existing chain-head race, and that should be the next task.**
`_get_last_entry_hash` reads the current head with a plain `SELECT ... ORDER BY occurred_at DESC
LIMIT 1` — no `FOR UPDATE`, no advisory lock — and there is **no unique constraint** on
`audit_log.prev_entry_hash` or on `evidence_custody_events (evidence_id, sequence_number)`
(verified against the initial migrations, not assumed). Two concurrent writers can therefore read
the same head and both insert, forking the chain into two valid-looking branches. That defect
predates this change, but signing adds a KMS round-trip *between the read and the insert*, which
widens the window from sub-millisecond to however long the KMS takes — milliseconds locally, a
network RTT with Vault. A forked ledger is not a hypothetical inconvenience on a court-facing
record. The fix is a uniqueness constraint on the chain link plus a retry, or serialization of the
append; it is a migration and belongs in its own increment rather than being bolted onto this one.

### What is still open

Signatures make **modification** detectable. They do nothing about **truncation or rollback**: an
insider who deletes the last N entries, or restores an older backup, leaves a shorter chain in
which every remaining entry verifies perfectly. Nothing inside the database can detect that,
because the evidence of the missing entries is exactly what was removed. Only ADR-0003 §3's
externally-anchored monotonic root closes it — Wave 1.3.

So **PRD SR-4 is substantially met but not complete.** "Tamper-evident even to an administrator
with direct database access" now holds for any modification to an entry that exists; it does not
yet hold for removal of entries. The honest description is *"tamper-evident and non-repudiable
against modification; not yet proof against truncation"*, and that is what the ADR and
`security-architecture.md` §20/§22/§23 now say. `security-architecture.md` §20 previously called
signing "an optional but recommended enhancement" — superseded for the two ledgers, while its
genuinely open question (per-examiner vs system-level key custody, which is a legal decision as
much as a technical one) is preserved and explicitly *not* resolved by this work. What is built
supports "this system attests", not "this examiner attests".

The Verification Engine (Wave 1.4) is still unbuilt. `LedgerSigner.verify` is the primitive it
will call, but no endpoint or scheduled job invokes it yet, so nothing is checking these
signatures in production — only the tests are.

**Tests: 38 new** (28 unit, 10 integration), all against real Ed25519 from the dev provider. A
stub would have made every tamper assertion vacuous, so `tests/fixtures/kms.py` builds the real
provider on a throwaway keystore removed at exit. Existing tests needed the KMS threaded through
46 service constructions across 13 files.

**Gates.** ruff (lint + format), `mypy --strict` (186 files), import-linter (2/2 kept), full suite
**805 passed / 2 skipped** (up from 767). Platform coverage **92.27%** against the 90% floor, with
`crypto/ledger.py` at **100%**.

**Known gap.** Rows written before this increment carry `signature = NULL`. They cannot be signed
retroactively with any honesty — a signature applied now would attest to bytes nobody witnessed at
the time — so they stay null forever and a verifier must report them as *not independently
verifiable*, distinct from both verified and forged. That distinction is already implemented in
`LedgerSigner.verify` (a missing envelope returns `False`, and callers check the column to tell
the two apart) and in the console's `partial` status from IC-027.

---

## 2026-09-08 — IC-029: chain-head race fix + external Merkle anchoring (Wave 1.3, ADR-0003 §3)

**Type:** Evidentiary-core correctness and security. One concurrency defect closed, one new
integrity mechanism built. Three migrations, two new platform modules, no API change.

### Part 1 — the chain-head race

IC-028 flagged this and made it worse; it is fixed here. Appending to a hash chain is a
read-modify-write with no atomicity: read the head, build an entry naming it, insert. Two writers
reading the same head both produce valid entries and the ledger becomes a **tree** — two
contradictory histories, each internally consistent, with nothing in the data to say which is real.
Signing widened the window from sub-millisecond to a KMS round-trip.

**Two mechanisms doing different jobs, and the distinction is the design.**

*Unique indexes make a fork impossible.* `audit_log(prev_entry_hash)` unique means each entry hash
has at most one successor — the structural difference between a chain and a tree. Custody gets the
same on `(evidence_id, prev_event_hash)`, scoped to the evidence item because every chain starts
from the same genesis sentinel and a global index would permit exactly one evidence item to exist,
plus `(evidence_id, sequence_number)` which CEM §4 has always *claimed* was monotonic without
anything enforcing it. This is the correctness guarantee, and it holds against a writer that
bypasses the service layer entirely.

*The advisory lock stops writers wasting work racing for that constraint.* `pg_advisory_xact_lock`,
taken before the head read, released on commit **or rollback** — a session-scoped lock leaked by an
exception would wedge the chain until the connection was recycled. Keyed per-chain via blake2b
rather than `hash()`, which is salted per process: two workers would otherwise compute different
keys for the same chain, and the lock would look present while doing nothing.

**Proven by mutation, not assertion.** Disabling the lock and re-running the suite produced exactly
the predicted result: `test_concurrent_appends_serialize_into_one_unbroken_chain` failed with an
`IntegrityError` from the unique index. The chain did not fork — it failed closed. That single
experiment demonstrates both halves at once, and it is why the tests are worth trusting.

The head query still orders by `occurred_at`, which is a heuristic — clocks are not monotonic. It
cannot cause a fork: an entry built on a non-head row collides with that row's real successor and
the transaction dies. Worth knowing, not worth a bigger change.

### Part 2 — external anchoring

**The gap, stated exactly: signatures prove no entry was *changed*; anchors prove none was
*removed*.** Delete the tail of a ledger, or restore yesterday's backup, and every surviving entry
still verifies, every signature is still valid, every link still points at a real predecessor.
Nothing inside the database can detect it, because the evidence of what is missing is exactly what
was removed. `test_truncating_the_tail_is_detected` asserts that internal consistency explicitly
before showing the anchor catching it anyway — the point is not that the chain looks broken, it is
that it looks perfect.

`platform/crypto/merkle.py` — RFC 6962-style, and the two properties a naive implementation gets
wrong are tested by name. Leaves are `0x00`-prefixed and interior nodes `0x01`, without which an
interior digest can be presented as a leaf. Odd nodes are **promoted, not duplicated**: duplicating
is CVE-2012-2459, where `[a,b,c]` and `[a,b,c,c]` produce one root. Order is committed to, not just
membership — a reordered custody chain is a different history.

`platform/crypto/anchoring.py` — builds the root, signs it under the evidence key, writes a
self-describing document to WORM. The document stands alone deliberately: an auditor holding only
the object and the public key can verify it without the database, which is the whole point, since
the database is the thing being checked. The object is written **before** the anchor row is
recorded; a row pointing at an object that was never written would be a database claiming an anchor
exists when it does not.

WORM is real Object Lock in **COMPLIANCE** mode, not GOVERNANCE — governance retention is
bypassable by a principal holding `s3:BypassGovernanceRetention`, which is precisely the privileged
insider being defended against. The storage port had no Object Lock support at all despite Wave 0.3
being marked built, so `ensure_worm_bucket` and `put_immutable` were added.

### Two architectural conflicts found and resolved

**1. `anchor_ref` cannot live on the ledger rows, and ADR-0003 §5 says it does.** An anchor exists
*after* the entries it covers, so recording it there is an `UPDATE` — which ADR-0004's append-only
trigger rejects unconditionally on exactly those tables. The two ADRs contradict each other.
Resolved in favour of ADR-0004 (never weaken append-only for bookkeeping convenience): the
relationship lives in a new append-only `platform.ledger_anchors` table keyed by entry-hash range.
The `anchor_ref` columns stay permanently null and are left in place rather than dropped, so a
verifier meeting an old row knows they were never populated. Recorded as an amendment to ADR-0003
rather than silently worked around.

**2. ADR-0003 §1's signed message is ambiguous as literally written.** `(sequence || prev ||
entry_hash)` stops being uniquely parseable the moment crypto agility does its job, since a SHA-256
digest is 64 hex characters and a SHA-384 is 96. Already built as canonical JSON in IC-028; the ADR
is now amended to say so rather than disagreeing with the code.

### What is deliberately NOT built: the RFC-3161 TSA client

Task 3 asked for a timestamping authority client. It is not here, and the reason is not effort.

WORM and a TSA defeat **different attacks**, and only one of them was the stated objective.
WORM makes an anchor *undeletable* — that is what closes truncation and rollback, and it is done.
A TSA makes an anchor *undatable-forward*, closing a narrower residual: an attacker holding both
the application and the clock publishing a fresh anchor over a doctored history and claiming it is
old. They still cannot replace an anchor already in WORM.

Building it properly means DER-encoding a `TimeStampReq` and, far harder, **verifying** the CMS
`SignedData` token that comes back. An unverified token proves nothing, and hand-rolling CMS
verification is exactly the "lighter version of a security control" `security-architecture.md`
warns against. It needs an ASN.1/CMS dependency (`asn1crypto` or `rfc3161ng`), which per CLAUDE.md
is an ADR-worthy decision — and it needs an answer for air-gapped deployments, which ADR-0003's own
Status already says cannot be closed generically because there is no reachable public TSA. That is
a scoped increment with a design decision in it, not a loose end.

`tsa_token_ref` is reserved and null. `ADR-0003`'s status table now lists the two halves of §3
separately so the distinction cannot be lost.

### Scope note

Anchoring is a **library plus a store, not a running job.** `LedgerAnchorService` and the anchor
table exist and are proven end to end, but nothing schedules batch cutting yet, and no endpoint
reports anchor status. That is the Verification Engine (Wave 1.4), which is where a scheduled
re-verification and a court-facing report belong. Today the mechanism is exercised only by tests.

**Tests: 75 new** (56 unit, 19 integration). The integration suite performs the real attacks
against a live Postgres: `DELETE` on the ledger after dropping the append-only trigger (which a
superuser can do — and which is exactly why ADR-0003 says the database alone is never the
guarantee), and a restore-to-older-snapshot.

**Gates.** ruff (lint + format), `mypy --strict` (192 files), import-linter (2/2 kept), full suite
**880 passed / 2 skipped** (up from 805). Platform coverage **92.11%** against the 90% floor;
`merkle.py` 100%, `anchoring.py` 98%, `ledger.py` 100%.

**Known gaps.** `chain_lock.py` sits at 71% line coverage in the *unit* run because taking a
Postgres advisory lock cannot be unit-tested without Postgres; it is fully exercised in
`test_chain_concurrency_db.py`, including a test that asserts the lock genuinely blocks a second
writer by checking event ordering. And the anchor bucket's Object Lock configuration is created but
never *verified* on an existing bucket — a pre-existing non-WORM bucket with the right name would
be silently accepted. That check belongs in the startup readiness probe, where a misconfigured
bucket should stop the deployment; it is noted in `deployment-architecture.md` and not yet built.

---

## 2026-09-25 — IC-030: the Verification Engine (Wave 1.4, ADR-0003 §6)

**Type:** Evidentiary-core capability. Waves 1.1-1.3 made tampering *detectable*; this makes it
*detected*. One new endpoint, one scheduled job, no schema change, no migration.

### The gap this closes, stated exactly

Every guarantee the previous three waves built was **constructive**. Entries were correctly hashed,
correctly signed, and correctly anchored — and nothing ever read them back. A forged or truncated
ledger sitting in the database was indistinguishable from an intact one to anyone who never checked,
which meant the honest description of the platform was "we could prove this in court if someone
asked", not "we would know". `LedgerSigner.verify` existed as a primitive with no caller.

### Three layers, and why none of them is redundant

`platform/crypto/verification.py` runs all three per entry and merges the findings:

1. **Link continuity** — each entry names its predecessor's `entry_hash`, and sequence advances by
   exactly one. Catches a removed or reordered *interior* entry. No keys, no network.
2. **Entry authenticity** — recompute `entry_hash` from the row's own persisted fields (catches an
   edited column), then verify the signature envelope (catches an attacker who recomputed the whole
   chain). The second is not optional: nothing about a hash requires a secret, so an attacker with
   write access defeats layer 1 and the hash half of layer 2 together.
   `test_a_recomputed_chain_is_still_caught_by_the_signatures` performs exactly that attack against
   a live database — rewriting every hash and link downstream of a forged row — and asserts the
   chain comes out structurally flawless before showing the signatures catching it.
3. **Anchor inclusion** — recompute each published Merkle root over what the ledger holds now.
   Catches a deleted **tail**, which layers 1 and 2 structurally cannot: delete the last N entries
   and every survivor still links, still hashes, still verifies. Asserted in two halves for the same
   reason as IC-029: first that the truncated chain verifies perfectly without anchors, then that the
   anchor catches it anyway.

### Three states, because two would be dishonest

`verified` / `partial` / `failed`. The middle state carries rows written before Wave 1.2, which hold
`preimage_version = NULL` and `signature = NULL` and can never be signed retroactively — the bytes
that should have been signed are gone. Collapsing `partial` into `failed` would raise a tampering
alarm over honest history on every run, which is how a real alarm gets muted; collapsing it into
`verified` would whitewash an unsigned row. `partial` deliberately does **not** trip the job's alarm.

Three more findings land in `partial` for the same reason: an unknown `preimage_version` (written by
a future writer — guessing its field set would report a valid entry as forged), a `hash_algo` this
build cannot compute (crypto agility means history stays verifiable under the algorithm it was
*written* with), and a row whose preimage is unavailable at all.

### The engine is pure, and that was forced as much as chosen

It touches no database and knows nothing about evidence or cases. The import DAG requires it:
`platform` may not import a module, so the custody ledger's field set has to arrive *from*
`modules.ingestion`. `custody_preimage_fields` and `audit_preimage_fields` were extracted from the
writers and made public for that — one function per ledger serving both directions, because two
copies of a preimage would drift, and the drift would surface years later as a chain that stops
verifying for no discoverable reason.

It also made the tests possible: every verdict is provoked in `tests/unit/test_verification.py`
without a database, and then re-proven against real tampered rows in
`tests/integration/test_verification_db.py`.

### Two scope decisions worth recording

**The audit ledger is verified in two different scopes, and conflating them was a real bug I nearly
shipped.** Entry-level checks run over a bounded window (default 5,000) because that ledger grows
without limit. Anchor checks run over the **whole** chain's hash list, because an anchor exists to
catch a deleted tail and a deleted tail is by definition not inside a window of surviving rows.
Checking anchors against the window would report every older anchor as a missing range — a permanent
false alarm — while missing the one thing anchors are for. `verify_chain` therefore takes
`chain_entry_hashes` separately from `entries`, and two unit tests pin both directions (the correct
behaviour, and the false alarm the separation prevents).

**The scheduled job writes no audit entry per chain it checks.** Every audit write is itself an
append to the other evidentiary ledger: it takes the chain lock, costs a KMS signature, and adds a
row. A sweep over 250 custody chains would write 250 audit rows per run, forever — audit-log
amplification driven by a process that discloses nothing to anybody, and which would in time
dominate the ledger it is meant to be watching. Safe to omit because it reads ledger metadata, never
payloads. Anything a *user* reaches still goes through the audited path.

### What "alarm" means, and what it deliberately is not

Prometheus metrics (`sentinelai_ledger_verification_state` as a gauge, so an alert rule can ask "is
this ledger broken *now*" rather than counting history) plus a `CRITICAL` structured log line.

**Not the notification module**, and this was checked before being decided rather than assumed.
Every dispatch path there requires an explicit `recipient_user_id` from the event payload, and
`notification/events.py` says outright that a handler "cannot invent someone to notify." A ledger
integrity failure has no user in its domain. There is no by-role user lookup anywhere in the
codebase and `NotificationRule` resolution is still `NotImplementedError`, so a handler for this
would resolve zero recipients on every firing — code that looks like alerting while reaching nobody.
Recorded as an ADR-0003 amendment so it is not "fixed" later by someone who assumes it was an
omission. No `integrity.verification_failed` event type was added; §25's catalog is unchanged.

The job completes normally after finding tampering. Raising would make arq retry a run whose verdict
will not change and eventually dead-letter it, turning a standing alarm into silence.

### A bug the tests found

The engine caught `LedgerPreimageError` around the hash recompute but not `CanonicalizationError`,
which is what `canonicalize` actually raises for an unrepresentable value. A single corrupt field
would have propagated and denied a verdict on the *entire* chain — a denial-of-proof an attacker
could trigger deliberately. Found by writing the test for it, not by reading the code.

### Endpoint

`GET /api/v1/evidence/{evidence_id}/verify`, documented as `api-design.md` §5.1. Distinct from the
already-shipped `POST .../verify-integrity`, and both exist deliberately: that one re-reads the
stored payload and recomputes its content hash (ADR-0008 §3, one object, mutates the ledger); this
one verifies the custody ledger (ADR-0003 §6, read-only). An intact payload on a forged ledger and
an intact ledger over a corrupted payload are both possible and both matter. A `failed` verdict
returns `200` — the request succeeded and the report is the answer; an error status would leave a
client unable to tell "this chain is broken" from "verification could not run".

### Tests and gates

**Tests: 47 new** (32 unit, 15 integration), all against real Ed25519. The integration suite
performs real attacks on a live Postgres after dropping the ADR-0004 trigger: rewriting
`actor_role`, rewriting the JSONB `details`, transplanting another entry's signature, recomputing
the entire chain, deleting the tail, deleting an interior row, and forging an anchor row. It also
carries the **false-positive control** — rewriting `details` with identical content in a different
key order must still verify, which is the test that justifies JCS over `json.dumps` and would fail
loudly if canonical encoding ever regressed.

**Gates.** ruff (lint + format), `mypy --strict` (195 files), import-linter (2/2 kept), full suite
**926 passed / 1 skipped** (up from 880/2; the one skip is the opt-in KMS benchmark). Platform
coverage **91.94%** against the 90% floor, `verification.py` at **98%**.

**Known gaps.** `ledger_verification.py` reads 50% in the *unit* run because its queries need
Postgres; they are covered in `test_verification_db.py` — the same pattern as `chain_lock.py` in
IC-029. The audit ledger's chain order still comes from `occurred_at`, a clock-based heuristic; it
cannot mask tampering (a reordered pair changes the root either way) but a clock inversion could
surface as an anchor mismatch on an intact ledger, and closing it needs a monotonic sequence column
on `audit_log` — a schema change, so its own increment. And nothing still *cuts* anchor batches
(IC-029's gap, unchanged): the verification engine checks whatever anchors exist, and in a
deployment where no batch job runs, that is none.

---

## 2026-09-25 — IC-031: anchor batch cutting + WORM startup enforcement (ADR-0003 §3, operational)

**Type:** Operational completion of the evidentiary core. Two scheduled/boot-time components, one
config addition, no schema change, no migration. **Two latent defects in IC-030 found and fixed.**

### What was actually broken

IC-029 shipped anchoring as "a library plus a store, not a running job". IC-030 shipped a verification
engine that checks whatever anchors exist. Nothing ever created one. So the state before this increment
was: a correct Merkle implementation, a correct WORM writer, a correct verifier — and, in any real
deployment, **zero anchors**, which means the truncation defence was fully built and completely
unarmed. Every anchoring test in the repository published its anchor by hand.

### The cutter

`modules/ingestion/anchor_jobs.py`, registered at `hour={0,4,8,12,16,20}`. Reads each ledger in its
canonical global order, takes the contiguous tail no anchor covers, publishes one anchor over it.
Three properties carry the weight:

**Contiguous, forward-only intervals.** An anchor commits to a range of one ordered list, so scattered
gaps are not anchorable; the boundary only moves forward, resolved as the *furthest* anchored position
rather than the newest anchor by timestamp (two concurrent cuts must not move it backwards).

**A 15-minute watermark.** Ordering is by `occurred_at` and clocks are not monotonic. Without the lag,
a write whose clock ran behind could commit *after* a cut but sort *before* its boundary, changing the
recomputed root for a ledger nobody touched — a false tampering alarm on healthy data, which is
precisely how a real alarm gets muted.

**An unresolvable boundary refuses the cut.** This one I got wrong first and the integration test
caught it. If an existing anchor's `last_entry_hash` is gone from the chain, the ledger has been
truncated. My initial `_unanchored_tail` logged a warning and left the boundary at `-1` — meaning
"anchor everything" — which would publish a fresh, correctly-signed anchor over the surviving doctored
history. That does not merely fail to detect the truncation, it **launders it into proof**. Only the
`uq_ledger_anchors_ledger_range` unique index stopped it, as an `IntegrityError`. Now the function
returns `None` (refuse) as a state distinct from `[]` (nothing new), and the job skips that ledger with
a `CRITICAL`-adjacent error, leaving the failing anchor on record for the verifier.

### Two latent IC-030 defects this exposed

**1. Custody verification would have broken the moment a second item was anchored.** IC-030's
`verify_custody_chain` passed only that evidence item's entries as the chain, while
`read_anchor_views` returns *every* custody anchor. `platform.ledger_anchors` has no column scoping a
range to a subject, so item Y's anchor endpoints do not appear in item X's chain — and would have been
reported `ANCHOR_RANGE_MISSING`: a false tampering verdict on every evidence item. It never fired
because no anchors existed. Fixed by cutting custody anchors over a **global** order
(`occurred_at, custody_event_id` — the tiebreak is required for a reproducible Merkle root) and passing
that global hash list as `chain_entry_hashes`. The alternative, a subject column on `ledger_anchors`,
is a schema change and would have been an invented field; recorded as the scaling trade-off it is.

**2. `unanchored_entries` was counted over the wrong set.** It iterated `chain_entry_hashes`, so once
that became the global ledger a per-evidence report would have quoted a figure for the entire custody
ledger. Now counted over the entries the report actually covers.

Both were found by building the thing that would have triggered them, not by re-reading the code.

### WORM enforcement at boot

`platform/storage/worm.py`, in the startup path of **both** entrypoints. The gap it closes is specific:
`ensure_worm_bucket` deliberately does not verify an *existing* bucket, because Object Lock is fixed at
creation and cannot be repaired by the application. So a pre-existing, correctly-named, non-WORM bucket
is the dangerous case — creation is skipped, writes may succeed, and every anchor in it is deletable
with nothing in the data to reveal it.

Refuses a bucket without Object Lock, and refuses a GOVERNANCE default (bypassable by
`s3:BypassGovernanceRetention` — exactly the insider being defended against). A bucket with *no*
default rule passes, because `put_immutable` names COMPLIANCE per write; rejecting that would reject
the correct configuration. "We could not check" (unreachable endpoint, denied credentials) is kept
distinct from "it is misconfigured" — different operator responses, so different exception types.

**Fails closed in production; outside production it logs and gates `/startupz`.** That is a deliberate
departure from the literal instruction to "fail the boot sequence" and it matches the existing KMS and
bucket-bootstrap posture: hard-failing everywhere would stop every developer without a WORM-capable
MinIO from running the server, and a degraded start is already visible to Kubernetes through the
startup probe. `worm_ready` is now part of the `/startupz` gate.

### The MinIO WORM gap, closed

IC-029's `ensure_worm_bucket` and `put_immutable` had **zero test coverage** — every anchoring test used
`FakeObjectStorage`, which records a dict entry and has no notion of a lock. The entire truncation
guarantee rested on code CI never executed. Four new tests now run against live MinIO: Object Lock is
enabled on a WORM-created bucket, absent on an ordinary one (MinIO signals this by *erroring*, so the
adapter's translation is pinned too), the readiness probe accepts one and rejects the other, and a
COMPLIANCE-locked object's version **cannot be deleted** while retention holds, with bytes intact.

### Tests and gates

**Tests: 24 new** (6 unit probe, 4 live-MinIO WORM, 14 cutter — 9 against a live database, 5 unit on
the boundary calculation). The end-to-end assertion is the one that matters: after a real cut the
Verification Engine reports the ledger `verified` with `unanchored_entries == 0`, and after a
truncation of that same anchored range it reports `failed` with `ANCHOR_RANGE_MISSING`.

One test helper had to be deleted rather than fixed: it backdated `occurred_at` to get entries behind
the watermark, but `occurred_at` is *inside* the audit preimage, so the `UPDATE` changed every
`entry_hash` and the ledger legitimately failed verification. The helper was measuring its own
corruption. Those tests now pass `watermark_minutes=0`; the watermark has its own dedicated tests.

**Gates.** ruff (lint + format), `mypy --strict` (197 files), import-linter (2/2 kept), full suite
**952 passed / 1 skipped** (up from 928/1; the skip is the opt-in KMS benchmark). Platform coverage
**91.92%** against the 90% floor; `worm.py` 100%, `verification.py` 100%.

### Known gaps

**Worker availability is now an evidentiary control.** No worker means no anchors, and entries written
in that window are permanently uncommitted — a deployment profile that scales the worker to zero
silently disables truncation detection. Recorded in `deployment-architecture.md` along with the signal
to alert on (`sentinelai_ledger_unanchored_entries` growing monotonically). Steady state should show a
small, non-zero, non-growing count; zero is not the target, unbounded growth is the alarm.

**Verifying one custody chain now reads the whole custody ledger's hash column**, a consequence of the
global anchor order. One indexed text column, acceptable at current scale; removing it needs a subject
column on `ledger_anchors` and is its own increment.

**Still open in Wave 1:** RFC-3161 timestamping (1.3c), which closes backdating only. `tsa_token_ref`
remains explicitly null. And the audit ledger still has no monotonic sequence column, so a clock
inversion wider than the watermark could surface as an anchor mismatch on an intact ledger.

---

## 2026-09-25 — IC-032: RFC 3161 timestamping (Wave 1.3c, ADR-0003 §3) — Wave 1 complete

**Type:** Evidentiary-core capability, the last item in Wave 1. One new dependency, one new platform
module, integration into the anchor cut and the Verification Engine. No schema change, no migration.

### What this closes

WORM made an anchor *undeletable*, so truncation is detectable. It said nothing about *when* the
anchor was made. An attacker holding both the application and the clock could doctor history, publish
a fresh anchor over it, and claim it was old. They could never replace an anchor already in WORM — so
this was a narrow residual rather than the hole truncation was — but it was the last one.

A timestamp token closes it because the attesting signature is made by someone else, over our digest,
at a time we do not control.

### The dependency, and why the choice was not obvious

`cryptography` was already a dependency and cannot do this: its `pkcs7` module signs, decrypts and
loads certificates, but exposes **no CMS verification entry point**, and has no TSP support at all. I
checked before adding anything, because if it could, hand-assembling verification would have been the
wrong call.

Chose `asn1crypto`, used **only as a DER codec**. Pure Python, no build toolchain, so it installs
identically into an air-gapped mirror; ships complete `tsp` and `cms` definitions; it is the ASN.1
layer beneath `oscrypto`/`certvalidator`. Rejected `rfc3161ng` (pulls `pyopenssl`, thinly maintained
for an evidentiary path) and `pyasn1` plus hand-written schemas (hand-writing ASN.1 for a security
boundary is the "lighter version of a security control" the security doc warns about). Every signature
check and certificate parse stays in `cryptography`. Recorded as an ADR-0003 amendment per CLAUDE.md.

One asn1crypto quirk cost real debugging and is now documented: `TimeStampResp` declares
`timeStampToken` **required**, while RFC 3161 §2.4.2 makes it OPTIONAL. A real rejection carries
status only, so parsing one through asn1crypto's class raises — and the only useful diagnostic, *why*
the TSA refused, surfaced as "malformed response". A test caught it. `tsa.py` defines a lenient
structure for that case.

### Verification is the whole module

Obtaining a token is an HTTP POST. An unverified token proves nothing — it is bytes a hostile server
or proxy can return at will. So `verify_timestamp_token` checks: response status; CMS `signed_data`
wrapping `tst_info` with content present (not detached); the timestamped digest is *our* digest under
the algorithm the token names; the nonce matches; exactly one `SignerInfo`; the `content-type` and
`message-digest` signed attributes bind the signature to *this* TSTInfo; the signature over the DER
`SignedAttrs` verifies; the signer's EKU is `id-kp-timeStamping` and **nothing else**, marked critical
(RFC 3161 §2.3); the certificate was valid at `genTime`; and it chains to a configured trust anchor.
SHA-1 imprints are refused outright — a timestamp over a collidable digest attests to nothing even
when the token itself verifies.

Two subtleties that a naive implementation gets wrong, both now pinned by tests. The signature covers
the DER **SET OF** `SignedAttrs`, not the implicit `[0]` tagging it carries inside `SignerInfo` —
`untag()` is what produces the bytes that were actually signed, and signing the tagged form fails
against every real TSA. And `ParsableOctetString.native` **auto-parses** the encapsulated content into
a dict; the message-digest attribute covers `.contents`, the raw DER. Using `.native` there would
digest the wrong bytes — which is how you get a verifier that accepts everything.

### Limits, stated rather than implied

**No revocation checking.** No CRL, no OCSP. Both need network calls an air-gapped deployment cannot
make and that would turn verification of *archived* evidence into a live-connectivity problem. A token
signed by a certificate revoked after issue still verifies here. The mitigation is operational: the
trust anchor set is the deployment's control surface. Path building is bounded the same way — direct
to an anchor, or through at most the intermediates the token carried.

**Air-gapped deployments keep the backdating residual by design.** Timestamping is off by default, and
an air-gapped or classified profile with `TSA_ENABLED=true` **fails to start**. That check runs for
*every* profile, not just production: a developer running the air-gapped profile is usually doing so
to prove the absence of egress, and a silently-ignored TSA URL would make the exercise worthless. RFC
3161 requires reaching a third party; an air-gapped deployment must have no path to one. Those
deployments keep exactly the description this ADR gave every deployment before today.

### Two decisions about failure

**Timestamping fails OPEN — the only thing in `publish` that does.** An unsigned anchor or an
unwritten WORM object is a lie, so both abort. An untimestamped anchor is a weaker *true* statement.
Aborting because someone else's TSA is down would let a third party's outage stop this platform
committing to its own evidence.

**A missing token is not a finding; an invalid one is a failure.** Every pre-1.3c anchor and every
air-gapped anchor carries no token, so degrading those to `partial` would flip correct ledgers to a
hedged verdict permanently and drain the meaning from the one state that means "this cannot be
proven". Reported as `AnchorFinding.timestamped` plus a report-level `untimestamped_anchors` count. A
token that is present and does not verify is `tsa_token_invalid`, a hard failure — downgrading a
forgery to "no timestamp" would mean an attacker could attach junk and lose nothing. A token present
with an *empty* trust store also fails: "we cannot check this" is not "this is fine".

### Tests

**38 new** (24 TSA lifecycle/verification, 8 HTTP transport, 11 anchor integration, 4 persisted-DB —
overlapping counts across files). `tests/fixtures/fake_tsa.py` mints **real** CMS `SignedData` with a
real certificate chain and real RSA signatures; a stub returning canned bytes would have made every
rejection assertion vacuous. Each fault is injected individually — wrong digest, replayed nonce,
missing EKU, non-critical EKU, extra EKU, expired certificate, absent certificates, corrupted
signature, untrusted chain, empty trust store, garbage DER, rejection response — because verifying a
good token proves almost nothing on its own.

The HTTP client is driven through `httpx.MockTransport`, not a socket: a test reaching a public TSA
would be slow, flaky, and a genuine egress path in a build whose point is that air-gapped works.

One helper had to be deleted rather than fixed, for the same reason as IC-031's: backdating
`occurred_at` to move entries behind the anchor watermark changes the audit preimage and breaks every
`entry_hash`, so the test would have been measuring its own corruption.

### Gates

ruff (lint + format), `mypy --strict` (198 files), import-linter (2/2 kept), full suite **990 passed /
2 skipped** (up from 952/1; the extra skip is the Vault contract test, whose container I removed after
IC-031). Platform coverage **91.42%** against the 90% floor; `verification.py` 100%, `anchoring.py`
99%, `tsa.py` 84% — the uncovered remainder in `tsa.py` is defensive branches for malformed ASN.1
shapes the fake TSA cannot construct.

### Wave 1 is complete

Every item in `docs/modernization-roadmap.md` Wave 1 — the evidentiary gate — is now built, running,
and verified: canonical encoding (1.1), complete signed preimages (1.2), server-computed integrity
(1.5), chain-head serialization (1.3a), Merkle/WORM anchoring (1.3b), RFC 3161 timestamping (1.3c),
and the Verification Engine with its scheduled re-verification (1.4). The next roadmap item is Wave
2.1, the request-scoped transaction boundary (ADR-0005).

**Standing gaps carried forward, unchanged by this increment:** no worker means no anchors (IC-031);
verifying one custody chain reads the whole custody ledger's hash column (IC-031); the audit ledger
has no monotonic sequence column, so a clock inversion wider than the cutter's watermark could surface
as an anchor mismatch on an intact ledger.

---

## 2026-09-25 — IC-033: Wave 1.3c committed; Wave 2.1 transaction boundary (ADR-0005)

**Type:** Release of the completed evidentiary core, then the first Wave 2 increment. One new platform
module, twenty-four deletions across nine routers, no schema change, no migration.

### Wave 1.3c shipped

RFC 3161 timestamping (IC-032) committed as `7e3116d` and pushed. **Wave 1 — the evidentiary gate —
is complete**: canonical encoding (1.1), complete signed preimages (1.2), server-computed integrity
(1.5), chain-head serialization (1.3a), Merkle/WORM anchoring with a scheduled cutter (1.3b), RFC 3161
timestamping (1.3c), and the Verification Engine with scheduled re-verification (1.4).

### Wave 2.1: what was actually wrong

ADR-0005's Context claimed 8 `self._uow.commit()` sites in `case_management/service.py` and warned
about workflows producing two independent commits. **Neither was true any more.** A survey of every
`.commit()` call in `src/` found none in any service: they sat in routers, job wrappers, the CLI, the
event dispatcher and the auth router — all entrypoints. §2 was already satisfied, and §3 was too
(every module's `OutboxWriter` already shares the service's session).

§4's premise did not hold either. `POST /evidence/batch` runs the whole batch in **one** transaction;
its per-item `207` results come from pre-flush domain validation, not per-item commits. There were no
implicit per-item commits to remove and no savepoints to add. A per-item *database* error still aborts
the whole batch, which is the correct outcome — it stops a `207` body claiming success for rows that
were never committed.

So I corrected the ADR's Context rather than implementing a fix for a problem that no longer existed.

**What did remain was §1, half-done.** The commit was at the entrypoint, but hand-written in every
handler — twenty-four `await uow.commit()` calls — and rollback was implicit, resting on
`AsyncSession.close()` discarding an uncommitted transaction rather than being stated anywhere.

That shape is a silent-data-loss hazard, and it is worth being precise about why. A handler that omits
the call does not fail, log, or raise. The session closes, the transaction is discarded, and the
endpoint returns `201` describing a row that does not exist. Nothing in the type system or the test
suite notices unless some test happens to assert persistence. Twenty-four opportunities to make that
mistake, and one more with every new endpoint.

### The boundary

`platform/db/transaction.py`: a `TransactionalRoute(APIRoute)` that commits once on success and rolls
back on any exception, plus a `bind_session` router-level dependency that publishes the request-scoped
session for it. Attached to all nine routers; the twenty-four handler commits are gone.

**A route class rather than the `yield` dependency ADR-0005 §1 suggests, and the reason matters.**
FastAPI runs dependency teardown *after* the response has been produced, so an exception from
`commit()` there cannot become a `500` — it escapes with the response already begun, bypassing the
registered exception handlers and the standard error envelope. The client is told the write succeeded
when the transaction never committed. I verified that empirically with a throwaway app before
designing around it, rather than following the ADR's wording into a bug; the deviation is recorded as
an implementation note on the ADR.

**A router-level dependency binds the session rather than `get_session` doing it**, because dependency
overrides have to keep working. Tests routinely override `get_session` with a fake; a binding done
inside the real `get_session` would simply not happen, and the boundary would then quietly commit
nothing — the exact failure mode it exists to remove. The login test, which asserts commits against a
fake session, passes unchanged, which is the evidence that this works.

**Three endpoints still commit explicitly, and must.** A rejected ingest keeps its intake record and
`evidence.validation_failed` event (§25.2), a failed integrity check keeps its MISMATCH custody entry
(ADR-0008 §3), and a failed login keeps its `login_failed` audit row (security §5). Each commits and
re-raises; the boundary's rollback then finds an already-committed transaction and does nothing. I
considered an exception-class allowlist (`persists_writes = True` on `DomainError` subclasses) and
rejected it: which writes survive which failure is a decision belonging to the endpoint that made
them, not a global property of an HTTP status code.

### Tests

**10 new**, all against a real Postgres, because the guarantee under test is *absence* — what the
database holds after a failed request — and a fake session would report whatever it was told. Each
path asserts the audit entry **and** the outbox event together, since §16 makes them one atomic unit
and a boundary that committed one but not the other would publish an event with no fact behind it.

Covered: a successful request commits both with no handler commit anywhere; an unhandled exception
after writing leaves nothing; a domain failure leaves nothing; a deliberate pre-commit survives the
error response; a rolled-back request does not poison the next one on a pooled connection; a read-only
route needs no transaction; and a failing commit does not report success — the property Deviation 1
exists for.

**One test was initially vacuous and I caught it by trying to make it fail.**
`test_every_api_route_attaches_the_transaction_boundary` filtered `app.routes` directly and found
*zero* `/api/v1` routes, so it passed while proving nothing: this FastAPI version does not flatten
`include_router` into `app.routes` — each inclusion is an opaque wrapper exposing `original_router`.
Walking that finds 77 routes, 73 guarded. The test now asserts a minimum route count before checking,
so the vacuous shape cannot come back, and a companion test states that the four unguarded routes are
exactly the health and metrics probes — an exclusion by decision rather than by oversight.

### Gates

ruff (lint + format), `mypy --strict` (199 files), import-linter (2/2 kept), full suite **1008 passed
/ 2 skipped**. Platform coverage **91.35%** against the 90% floor; `transaction.py` 86% (the
uncovered remainder is the rollback-failed and no-session-bound defensive branches).

### Scope note

The worker job wrappers and the event dispatcher already owned their transactions in the shape §1
requires and were left alone. Wave 2.1's remaining ADR-0005 items are closed by correction rather than
by code: §2/§3 were satisfied before this increment, and §4's per-item-commit premise does not apply
to the batch endpoint as built. The next roadmap item is Wave 2.2 — dispatcher to worker with
`SKIP LOCKED` and per-aggregate ordering (ADR-0006).

---

## 2026-09-25 — IC-034: Wave 2.1 committed; Wave 2.2 dispatcher relocation + SKIP LOCKED (ADR-0006)

**Type:** Release of Wave 2.1, then the event-transport increment. Eight migrations, one new
entrypoint module, no schema change beyond indexes.

### Wave 2.1 shipped

Request-scoped transaction boundaries (IC-033) committed as `b6414d6` and pushed. ADR-0005 Accepted.

### Wave 2.2: what was wrong

The relay started in **every HTTP replica's lifespan** and polled with a plain
`SELECT ... WHERE dispatch_status='pending'`. N replicas therefore ran N uncoordinated pollers over one
set of tables: duplicate delivery (masked by inbox dedup, not prevented), wasted database load, and no
ordering guarantee at all despite §18 claiming per-aggregate order.

### The claim, and why the row lock alone is not enough

`FOR UPDATE SKIP LOCKED` stops two dispatchers *selecting* the same row concurrently. It does not stop
the second one selecting it a moment later: the lock is released when the claim transaction commits,
and a row left `pending` while its handlers run is claimable again on the next poll — the same
double-dispatch, arriving half a second later. I nearly shipped exactly that.

So the claim transaction also stamps `last_attempted_at`, and the claim query skips rows stamped
within `CLAIM_LEASE_SECONDS`. The stamp is a **lease** that outlives the lock.

A `dispatching` status was the obvious alternative and is worse: a dispatcher killed mid-batch strands
rows in it, needing a reaper and a "how long is too long" threshold. An expired lease needs neither —
the row simply becomes claimable. At-least-once is unchanged; post-crash redelivery is absorbed by the
Inbox guard as always. And the same gate *is* ADR-0006 §4's retry backoff: one mechanism, two
requirements.

### Ordering without a lock

ADR-0006 §3 offers "advisory lock or hash-partition of schemas". Neither was needed. The claim takes
**the oldest pending row per `aggregate_id`**, so a batch can never hold two events for one aggregate.
Ordering across dispatchers then falls out of that plus the lease: while event 1 for aggregate X is in
flight it is still `pending` and still leased, so it remains the oldest pending row for X — X yields
nothing, and event 2 cannot be claimed until event 1 resolves. No lock to acquire, none to leak, no
partition assignment to rebalance when a replica dies.

`DISTINCT ON` sits in a subquery because Postgres rejects `SELECT DISTINCT ... FOR UPDATE` outright.

### Relocation

`entrypoints/consumers.py` now holds the registrar list. It was in `http/main.py` because the
dispatcher ran there, and copying it into the worker would have been the obvious mistake: two lists
drift, and the failure mode is a handler that silently never runs in production because only the
process that no longer dispatches knew about it. The worker drains the relay **before** disposing the
engine — the relay holds sessions, and tearing the pool out from under an in-flight handler would
abort it mid-transaction, which §2.2 forbids.

### Two defects the tests caught

**Revision ids too long.** Alembic stores `version_num` in a `varchar(32)`;
`202609250001_ingestion_dispatch_idx` is 35 characters, so the migration's final
`UPDATE alembic_version` failed — not the migration body, which made the error read as unrelated.
Shortened to `_idx`, and the reason is recorded in each migration's docstring so nobody lengthens
them back.

**Two commit-count assertions in `test_event_plumbing.py` were correct and became wrong.** The claim
transaction adds a commit per drain, so 1→2 and 3→4. Updated with a comment naming the extra commit as
the lease, because a bare number invites someone to "fix" it back.

### One unexplained run, stated rather than buried

The first execution of the new concurrency suite failed with every event delivered **twice** (24 for
12). I could not reproduce it: the same test passes in isolation, the whole file passed four
consecutive runs, and a direct probe of two concurrent claims showed 6/0 with zero overlap. The
compiled SQL verifiably contains `FOR UPDATE SKIP LOCKED`.

Rather than shrug, the test is now stronger than the one that flaked: it polls repeatedly instead of
once (many interleavings, not one), and it asserts the **invariant directly** — the two dispatchers'
claimed id sets must be disjoint each round — so a recurrence points at the claim rather than at a
delivery count. I have no explanation for that run and am not claiming one.

### Tests

**11 new**, all against real Postgres, because `SKIP LOCKED` is a guarantee the database provides
*between sessions* — a fake or a shared session cannot exhibit it, and a test that appeared to prove
deduplication without real row locks would prove nothing.

Covered: no double-dispatch under concurrent polling (with disjointness asserted per round); a leased
row is invisible to a peer claim; an expired lease makes an orphaned row claimable again; only the
oldest pending row per aggregate is claimed; per-aggregate delivery is oldest-first, including with two
dispatchers competing on one aggregate; distinct aggregates still batch together (ordering is per
aggregate, not global); a failed row waits out its backoff; it retries once the window passes; it
dead-letters at the ceiling; terminal rows are never re-claimed.

### Gates

ruff (lint + format), `mypy --strict` (208 files), import-linter (2/2 kept), full suite **1019 passed
/ 2 skipped**. Platform coverage **91.31%** against the 90% floor; `dispatcher.py` at **99%**. The
migration round-trip (upgrade-head → downgrade-base, every schema) passes with the eight new indexes.

### Carried forward

With no worker running, the API writes outbox rows that nothing relays — events are not lost (rows
stay `pending`) but nothing downstream reacts. Worker availability is now a correctness concern, the
same shape as the anchor-cutter dependency from IC-031, and §28's `_outbox_pending_count` /
`_oldest_pending_age_seconds` are the signal. Recorded in ADR-0006 and event-driven §2.

Next roadmap item is Wave 2.3 — event authentication via signed outbox rows (ADR-0007).

---

## 2026-09-28 — IC-035: Wave 2.2 committed; Wave 2.3 event authentication (ADR-0007)

**Type:** Release of Wave 2.2, then cryptographic non-repudiation for inter-module eventing. Eight
additive migrations, one new platform module, no breaking change to any event contract.

### Wave 2.2 shipped

Background dispatcher with SKIP LOCKED (IC-034) committed as `2229361` and pushed. ADR-0006 Accepted.

### Wave 2.3: what was wrong

The event bus authenticated nothing. An insider who could `INSERT` into a schema's `outbox_events`
could forge a domain fact — `evidence.superseded`, `case.status_changed` — and every consumer would
process it as authentic. The Inbox is no defence: it deduplicates on `(event_id, handler_name)` and a
forger mints a fresh `uuid4`.

### What was checked before building

`KeyPurpose.EVENT_ROOT` already existed and is reserved in ADR-0009 §7 for exactly this, so no key
hierarchy invention was needed. ADR-0007 does **not** mandate a verify-optional flag — the roadmap
lists one as this wave's *rollback* mechanism, which is the framing the implementation uses.

### Three outcomes, not two

§2 says reject a signature that is "absent or invalid". Implemented as three, because collapsing the
first two would be wrong in both directions:

* **verified** — delivered.
* **absent** — delivered only under `permissive`. A row written before this wave genuinely has no
  signature and cannot gain one; signing it now would attest to bytes nobody witnessed.
* **invalid** — never delivered, in either mode.

Tolerating a present-but-invalid signature under permissive would hand an attacker a downgrade:
corrupt the envelope and a forgery is treated as merely unsigned. Same distinction ADR-0003 §6 draws
between *not provable* and *forged*.

### Design decisions worth recording

**Verification runs before the inbox claim**, not after. The inbox deduplicates; it does not
authenticate, and claiming it first would also record a forged event as seen.

**Rejected events are quarantined, never retried.** A bad signature will not become good, so the row
goes straight to `dead_letter` with the reason in `last_error` (on the row, so an operator need not
correlate against logs) and a `CRITICAL` log line — under this ADR's threat model a forged event is an
insider fabricating a fact, not a delivery hiccup.

**The schema is inside the signed message**, which ADR-0007 §1 does not say. Without it a row lifted
verbatim from one module's outbox into another's carries a genuine signature over genuine content and
verifies. `test_an_event_copied_into_another_modules_outbox_is_rejected` is that case.

**Signing fails closed** inside the publisher's transaction, so a KMS outage aborts the business
write rather than committing an unauthenticatable fact — the trade ADR-0003 §1 makes for the ledgers.

**How the signer reaches a publisher.** `get_<module>_uow` injects the process KMS; job wrappers pass
`ctx["kms"]`. For events published *by a handler* (`notification.dispatched`) the dispatcher attaches
the signer to the handler's UoW, because the module-supplied `uow_factory` contract takes only a
session and threading a KMS through eight modules for a composition concern would be a wide change
for no gain. A publisher with no signer writes `NULL` — honest, and not silent, because strict mode
refuses the event at consume time.

### Three defects found while building

**The initial migration must not grow columns.** I first added `signature`/`key_id`/`sig_alg` to
`create_outbox_events` *and* wrote the additive migration. A fresh upgrade-to-head then created them
twice: `DuplicateColumn`. A migration is a historical record of the schema at its own revision, not a
description of the current shape — the current shape lives in `outbox.py`'s Core table. Reverted, with
the reasoning recorded in the helper so it is not re-added.

**`get_*_uow` depending on `get_kms` broke every API test**, because `get_kms` reads `app.state.kms`
which the HTTP lifespan sets and `ASGITransport` does not run. Twelve failures and a 16-minute suite
(rich traceback rendering). Fixed by overriding `get_kms` in the three API test app builders — the
documented testing seam, not a workaround: a request reaching a real publisher genuinely needs a
signing identity.

**The test KMS held only `EVIDENCE_ROOT`.** `EVENT_ROOT` is a separate key by design, so the fixture
now creates both; a KMS holding one would fail half the suite on a missing key rather than on anything
it asserts. Added `foreign_kms_for_tests()` (a second provider on its own keystore) for the
"signed by someone else" tests — built at import time, because `asyncio.run` cannot be called from
inside the running loop of an async test.

### Tests

**40 new** — 27 unit, 13 integration. The unit file varies **each signed field one at a time** and
asserts the signature stops verifying: a signature covering only the payload would pass an
end-to-end forgery test while leaving `event_type` and `actor_ref` freely rewritable, which are the
fields an attacker most wants. That table is what makes "the signature covers the event" checked
rather than claimed.

The integration file performs the real attack — a hand-written `INSERT` — plus tampering with the
payload, rewriting `event_type`, corrupting the envelope, transplanting a row into another module's
schema, and signing with a foreign key. Each is rejected, quarantined, and not retried. Permissive
mode is proven to carry an unsigned legacy row *and* to still refuse an invalid signature.

### Gates

ruff (lint + format), `mypy --strict` (217 files), import-linter (2/2 kept), full suite **1032 passed
/ 2 skipped**. Platform coverage **90.66%** against the 90% floor (`signing.py` 92%, `outbox.py`
100%). Migration round-trip green across every schema.

### Carried forward

**ADR-0007 §3 (writer restriction) is not built** and is recorded as such in the ADR's new status
table. With §1/§2 built and strict mode on, forging an event requires the signing key — the
substantive guarantee. §3 would additionally require the owning module's database role, making
cross-module forgery need two independent compromises; it belongs with an ADR-0004 grant narrowing.

Next roadmap item is Wave 2.4 — rich aggregates and value objects (ADR-0011).

---

## 2026-09-28 — IC-036: Wave 2.3 committed; Wave 2.4 rich aggregates and value objects (ADR-0011)

**Type:** Release of Wave 2.3, then a domain-model refactor. No migration, no schema change, no API
contract change — every error code, field name and message is preserved. Enforcement *moved*;
behaviour did not.

### Wave 2.3 shipped

The signed outbox (IC-035) committed as `0ca0543` and pushed. ADR-0007 is Accepted with §3 (writer
restriction) recorded as not built.

### Wave 2.4: what was wrong

Invariants that are *legal* guarantees lived in `if` statements inside service methods. The
legal-hold gate on disposal (security-architecture.md §39), the already-superseded check, the
review-once rule behind PRD FR-7.3's human-in-the-loop promise — each was enforced at exactly one
call site. A second code path that appended a custody event would have bypassed the hold gate
silently, and nothing in the type system or the model would have objected. `Case` was the lone
exception, made rich back in IC-011.

Alongside that, CEM vocabulary was raw `str` everywhere: an integrity hash was a string plus a
separate algorithm string with no guarantee they agreed, and a confidence was whatever the call site
remembered to validate.

### What was checked before building

`Case` already satisfies §1, so this wave did not touch it. Two ADR-0011 claims did not survive
checking:

**§1's method sketch presumes mutable state.** It proposes `record_custody(...)`, `supersede(...)`
and `apply_legal_hold(...)`. ADR-0004's trigger rejects an `UPDATE` on the evidence table outright,
and ADR-0015 makes `status` and `legal_hold` derived from the custody ledger — so an
`apply_legal_hold` that sets a column is a method that cannot exist here. The built shape expresses
the same rules for an append-only store: `assert_can_record_custody`, `apply_custody_event` (returns
the new hold state for the caller to write with `set_committed_value`, never dirtying the instance),
`assert_supersedable`.

**§1's "≥1 supporting evidence" is not a review rule.** The ADR's Context calls it "the CEM §13
'≥1 supporting evidence' rule", which reads as a rule about findings generally, and I implemented it
as a guard on confirmation. A unit test failed and sent me to the source: CEM §1.6 says "No Entity or
Relationship may **exist** without at least one supporting evidence reference" and §13's table says
*Reject*. It is an existence invariant, enforced at creation — which `create_relationship` already
did inline.

Checking it at confirmation was wrong twice over. Too late, because the unsupported row already
exists by then. And perverse, because it would refuse to let an analyst **reject** an unsupported
finding — the exact outcome the rule wants. The guard is now
`Relationship.assert_supporting_evidence(count)`, a classmethod (it is asked before the instance
exists) taking a count (the supporting rows are written in the same transaction; there is nothing to
query yet). CEM §13 grants entities an explicit exception — a pre-registered entity needs no
`MENTIONS` edge — so `Entity` creation deliberately does not call it. The repository's
`count_for_relationship`, added for the wrong design, was deleted.

### Two vocabularies that had already drifted

Writing the value objects surfaced a live defect. `shared/cem.py` defined
`PUBLIC_SOURCE_AUTHORITY = "public-source"` — a literal that appears **nowhere** in the CEM. §13's
validation table and both worked examples say `public_source_no_authority_required`, `apps/web`'s
`PUBLIC_SOURCE_SENTINEL` hardcodes that string, and the ingest validator has always accepted exactly
it. `LegalAuthorityRef.is_public_source` would have returned `False` for the only sentinel the API
accepts. The value object now quotes the model verbatim; it is a wire value in an evidentiary record,
not a name to restyle.

The same check found `CUSTODY_EVENT_TYPES` and the integrity-algorithm set defined in **both**
`shared/cem.py` and `ingestion/service.py`. With `CustodyEventType` performing the vocabulary check,
the service's copies were dead and free to diverge from the ones actually enforced. The service now
imports them and re-exports `CUSTODY_EVENT_TYPES` for its published surface. Both rejection messages
are byte-identical to before, so no client sees a change.

### The aggregates are the ORM classes

`Evidence`, `Entity` and `Relationship` follow the pattern `Case` set: behaviour on the declarative
class, not a parallel domain object behind a mapper. A declarative instance is an ordinary Python
object until it meets a session, so every invariant is exercised with no database, no fixtures and no
engine — the property ADR-0011's Consequences actually ask for. So the "aggregate↔ORM mapping layer"
the ADR anticipates was **not** built: it would add a translation step on every read and write plus a
second place for the shape of an evidence record to drift, to buy purity for invariants already
expressible where the data is.

`Entity` and `Relationship` share the review machine through a `_Reviewable` mixin rather than each
carrying a copy. They are separate aggregates with separate tables and separate revision ledgers, but
the rule is one rule, and two copies are two chances to drift on a guarantee the PRD makes
explicitly. Inside `review()` the vocabulary check comes **before** the state check, so a caller
sending nonsense is told it is not a disposition rather than that the finding is already reviewed —
which would be a confusing answer to a request that was malformed regardless of state.

### What the aggregates deliberately do not own

**Queries.** `assert_supersedable` takes a flag, `assert_supporting_evidence` takes a count. An
aggregate that loaded either needs a session, which makes it untestable without a database and hides
a query inside an invariant check. The query stays in the service; the decision is the aggregate's.

**ETag / optimistic concurrency.** Still checked in the service, before the aggregate is asked to
mutate anything. It is an HTTP concern with no domain meaning.

### Value-object decisions worth recording

**`IntegrityHash` length-matches the algorithm.** A 64-character digest labelled SHA-512 is not a
truncated SHA-512; it is a SHA-256 with the wrong label, and every verifier trusts the label.

**`CustodyEventType.legal_hold_state` is three-valued.** `None` means "this event says nothing about
holds". Collapsing it to a boolean would make every `accessed` event silently release a legal hold.

**`ConfidenceScore` refuses a `float` outright** rather than coercing. The column is `Numeric` and
scores are compared against thresholds; accepting binary floating point makes `0.7` a different
number in the domain than in the database.

**Category and artifact type validate shape, not membership.** The vocabulary is extended by
registering an attribute schema, so a closed enum would make adding a category a code change and
would reject data a correctly-registered connector may send. `CustodyEventType` **is** closed,
because every value has specific meaning to the custody rules — an unrecognised one is not
extensibility, it is an event nothing can reason about sitting in a legal record.

`shared/cem.py` rather than `ingestion/`: two modules need the vocabulary (`ingestion` for evidence,
`investigation` for confidence), and `shared` is the lowest layer in the import DAG, so neither module
depends on the other. `platform` has no reason to import it and does not — the DAG contract still
passes.

### Tests

**88 new pure unit tests** in `tests/unit/test_aggregates.py`: no database, no fixtures, no engine,
0.73 s for the file. The case machine is asserted over the **full status cross-product** rather than
the happy path, so an added status cannot quietly become legal. `test_investigation_status.py` now
imports `REVIEW_DISPOSITIONS` from the models, so the vocabulary has one definition in tests too.

A helper (`assert_rejects`) asserts on `ValidationFailedError.details` rather than `str(exc)`.
`ValidationFailedError` sets a fixed message and puts the reason in `.details`, so
`pytest.raises(match=...)` would have matched the fixed string and asserted nothing about the actual
rejection — a vacuous test. Checking the details pins the field name too, which is what an API client
reads.

### Gates

ruff (lint + format), `mypy --strict` (218 files), import-linter (2/2 kept), full suite **1147 passed
/ 2 skipped**. Platform coverage **90.66%** against the 90% floor; `shared/cem.py` **99%**.

### Carried forward

**ADR-0011 §3 is not built.** Aggregates do not raise domain events for the application layer to map
onto the outbox; publication is still a direct `outbox.publish(...)` from each service. That touches
every publisher in the codebase and is independently valuable, so it belongs in its own increment
rather than as a rushed half of this one. Nothing in §1 or §2 depends on it, and the guarantees that
matter for eventing — authenticity and per-aggregate ordering — are ADR-0006's and ADR-0007's, both
built. ADR-0011's status table records the split.

**No CEM change was needed.** The roadmap's Docs column for 2.4 reads "ADR-0011; CEM value objects",
which anticipated the model moving. It did not: the value objects quote CEM §4/§5/§13 rather than
extend them, and the one divergence found ran the other way — code that had invented a sentinel the
CEM never defined. Aligning the code was the fix.

**Wave 2 is complete.** 2.1 through 2.4 are all built and Accepted. Next roadmap item is Wave 3.1 —
authentication and sessions plus `case_members` (ADR-0010, XL), which is also the fix for defect D1.

---

## 2026-09-29 — IC-037: Wave 2.4 committed; Wave 3.1 access & API trust (ADR-0010, ADR-0017)

**Type:** Release of Wave 2.4, then authentication and authorization. One migration
(`case_management.case_members`), three new endpoints on `/auth`, three on `/cases`, and one new
ADR. Two live security gaps closed.

### Wave 2.4 shipped

Rich aggregates and value objects (IC-036) committed as `eaf8a71` (value objects + aggregate roots)
and `05ee47f` (service delegation + ADR-0011), then pushed. **Wave 2 is complete**: 2.1 `b6414d6`,
2.2 `2229361`, 2.3 `0ca0543`, 2.4 the two above. ADR-0011 is Accepted with §3 recorded as not built.

### What the roadmap asked for, and what the ADRs actually said

The instruction for this increment was to build `sessions.token_hash`, `case_members`, and MFA under
ADR-0010. Checking the documents first changed three of those four premises:

**`sessions.token_hash` was already built**, with `token_lookup`, the non-unique prefix index and
the argon2id digest. D1 has been closed since before Wave 1. No migration was needed.

**The MFA *storage* was already built too** — `202608300001_platform_mfa`, `mfa_challenges`,
`mfa_recovery_codes`, `MfaRepository`, and a vector-tested RFC 6238 implementation in
`platform/security/totp.py`. ADR-0010's own status block, written the day that migration landed,
still said "all MFA (no MFA columns exist)".

**`case_members` is not ADR-0010's** — amendment **A2** (2026-08-30) removed it and assigned it to
**ADR-0017 (Case Membership and Case-Level Access)**, which did not exist, and which A2 requires to
"be written before that work starts". Building the table under ADR-0010 would also have meant
inventing a table `database-design.md` §3.4 does not document, against `CLAUDE.md` rule 1.

So this increment wrote ADR-0017 first, and `database-design.md` §3.4 and `api-design.md` §4.2 were
extended in the same change as the code, not after it.

### Gap 1: MFA was storage with nothing reading it

`AuthService.login` issued a session on a correct password regardless of `mfa_enrolled_at`. An
account could complete enrolment, believe it held a second factor, and be protected by one — a
failure mode the user cannot detect and would not expect, and a standing violation of
`security-architecture.md` §8 ("mandatory for every role that can access evidence or case data — no
exceptions, per PRD SR-2").

Login now branches: an enrolled account gets an `mfa_token` (a short-lived, single-use credential
for a half-authenticated principal) and **no session row is written at all** until
`POST /auth/mfa/verify` succeeds. Two orderings inside that exchange matter:

* **The challenge is consumed before the code is checked.** Consuming only on success would let a
  stolen `mfa_token` be used to brute-force six digits until it expired. Consuming first makes each
  attempt cost a fresh password login.
* **Account status is re-checked at the second factor.** The window between password and factor is
  small, but an account disabled inside it must not walk through.

Recovery codes are accepted alongside TOTP, because a user who has lost their authenticator must
still be able to get in — refusing that produces lockouts, not security. The server never reveals
which of the two matched.

### Gap 2: ABAC was ownership-only, in two places

`security-architecture.md` §6 evaluates "case-scope grant" as its first ABAC attribute, and its
worked example is an investigator refused evidence "linked to a case they are not assigned to".
There was nothing to evaluate: `DbCaseAccessChecker` compared `cases.owning_user_id` to the caller,
so a case was reachable by exactly one person and every collaborative workflow in the PRD — a
forensic examiner working "multiple case teams", a supervisor reviewing findings — was
unimplementable.

`case_members` (composite PK `(case_id, user_id)`) is the grant. Access is **owner OR member**,
resolved in one `EXISTS` over the union rather than "fetch the owner, then maybe the membership" —
the two-query form makes the owner's check cheap and everyone else's cost an extra round trip, which
is backwards once a case has a team. The owner is deliberately **not** written into the table:
`owning_user_id` is already authoritative, and a duplicate membership row creates two places that
can disagree about who owns a case.

**The second place was the one that would have silently defeated the first.** `CaseService` had its
own `_load_owned` gate, ownership-only, called from thirteen sites. A granted member would have
passed `require_case_access` at the router and then been refused inside the service. It is now
`_load_accessible` and asks the same question the router's port does.

Membership needs a grant path or the table stays empty and ABAC stays ownership-only in practice, so
ADR-0017 adds `GET`/`PUT`/`DELETE /cases/{case_id}/members`. Every one of them is itself
case-scoped, which is what stops the endpoint group from being a self-service escalation path: a
caller who cannot open a case cannot add themselves to it. `PUT` is naturally idempotent — the
membership is named by the URL, so a re-grant updates the role in place — and the composite primary
key is what guarantees that rather than a convention.

### Gap 3: enforcement with no way to enrol

Found while checking that the new branch was reachable: **nothing called
`MfaRepository.store_secret`**. There is no enrolment endpoint (`api-design.md` §9 documents none)
and no CLI command, so no account could ever reach `mfa_enrolled_at` — the branch would never have
fired, and §8's "mandatory" factor would have stayed unmet while the code read as done.

`python -m sentinelai.cli.admin enroll-mfa` is the provisioning path, for the same reason
`create-user` is one: login could not be used until something could create a user, and no
admin-user endpoint is documented either. It prints the provisioning URI and ten recovery codes
once, audits the enrolment, and is deliberately **not** idempotent — re-running mints a new secret
and invalidates the registered authenticator, so it refuses an enrolled account without
`--replace`. Unlike `dev-token` it carries no production restriction: refusing to enrol a real
operator in production would make the mandatory factor unprovisionable exactly where it matters.

Recovery codes needed a generator, which also did not exist. `generate_recovery_code` uses a
Crockford-style base32 alphabet with `I`/`L`/`O`/`U`/`0`/`1` removed — a code is read off a screen
and typed back, and an alphabet holding both `0` and `O` guarantees support tickets. ~51 bits, less
than a session token and deliberately so: a longer code gets transcribed wrong, and each attempt
already costs an argon2id verify and is single-use.

### The denial is now recorded

`platform/auth/dependencies.py`'s module docstring claimed both RBAC and ABAC were "audited
regardless of outcome". Neither was. §6 requires the ABAC denial by name — "the denial itself is
written to `platform.audit_log` with the caller's identity, the resource requested, and the reason"
— because a compliance review has to distinguish "this analyst never had access" from "this analyst
had access and used it".

`require_case_access` now writes `case_access_denied` and **commits it before raising**. That commit
is not optional: ADR-0005's boundary rolls back on any exception, so without it the 403 would erase
the record that makes it interesting. Same composition the `login_failed` entry already used.

RBAC denials are still not audited, and the docstring now says so instead of claiming otherwise. No
document requires it, and it would put a KMS dependency in the path of every role-gated route to
record what the request log already carries.

### The developer seams needed testing, not removing

Both were already restricted. `issue_dev_token` refuses when `Settings.is_production`, which spans
`production`, `air-gapped` **and** `classified` because the latter two are hardening overlays on the
first. `apps/web`'s `VITE_DEV_ACCESS_TOKEN` sits behind `import.meta.env.DEV`, so it is dead-code
eliminated from a production build, and it never manufactures a session — the server still resolves
the token against a real `platform.sessions` row.

Neither guard had a test, which is the part worth fixing: a refactor dropping either would have
produced a build that mints sessions without a password on a classified deployment and failed
nothing. `tests/unit/test_dev_seam_guards.py` adds them, including a test that fails if a sixth
profile is ever added without being classified — the `app_env == "production"` shape of mistake that
would leave the two most sensitive profiles open.

### Defects found while building

**The API test harness would have hidden the whole feature.** `test_case_api.py` overrides the ABAC
port with an allow-all, which is right for testing case CRUD and fatal for testing access control.
The new `test_case_members_api.py` backs the port with the same store the service writes through, so
a grant is observable through the gate and a checker with its own state cannot hide a disagreement
between them.

**The denial audit dragged DB-less tests into Postgres.** Adding a real `record_audit_event` to the
403 path made `test_case_members_api.py` take 126 seconds and fail on a closed event loop, because
`get_session` was not overridden and every refusal reached for a real connection. The root conftest
now stubs that one symbol for DB-less tests (14s), and the denial audit is proven where it belongs,
against a real database. That stub then silenced the audit test itself — which is why
`test_case_access_audit_db.py` restores the real function in a module-level autouse fixture and says
why.

**The shared test KMS held two of three functional roots.** `SESSION_ROOT` (the encrypted TOTP
secret) was missing, so every MFA flow failed on `KeyNotFound` rather than on anything it asserted.
Added alongside `EVIDENCE_ROOT` and `EVENT_ROOT`, for the reason already written in that fixture.

### Tests

**48 new** — 16 session lifecycle, MFA enforcement and recovery codes (real Postgres), 7 membership
resolution (real Postgres), 4 denial auditing (real Postgres), 10 membership API, 11 dev-seam and
enrolment-wiring guards.

The ABAC tests are deliberately written against **both** predicates: membership on one case grants
nothing on another, and a membership belonging to one user does not stand in for another. A query
missing either predicate passes every other test in the file. The credential-scan test reads the
actual stored column values rather than trusting the write path, because "we hash it" is the kind of
claim that survives a refactor that stops being true. The recovery-code alphabet has its own test,
because "we excluded the confusable characters" is a property nothing else would notice losing.

### Gates

ruff (lint + format), `mypy --strict` (219 files), import-linter (2/2 kept — `platform` stays
domain-agnostic; the ABAC port is still a Protocol the composition root binds). Full suite
**1195 passed / 2 skipped**. Platform coverage **95.33%** (up from 90.66% — the new auth and access-control paths are densely tested) against the 90% floor. Migration round-trip green
including the new `case_members` revision.

### Carried forward

**SSO/OIDC/SAML is not built** — deferred by sequencing per ADR-0010 A1, explicitly *not* scoped out
of any profile (PRD SR-2 requires IdP integration, and §7 makes federation air-gap compatible
against an enclave-local IdP). `identity_provider_links` and `users.external_idp_subject` already
exist, so it costs no migration when it lands.

**A3's cookie transport is not built.** Refresh rotates — successor issued, predecessor revoked,
which is A3's security property — but there is one credential class, not two, and it travels in the
request body. Completing A3 needs a schema change, cookie issuance, and a matching `apps/web`
change in one coordinated increment. A3's CSRF argument still holds because no endpoint accepts a
cookie as authentication; the day one does, a CSRF token becomes mandatory in the same change.

**There is no self-service MFA enrolment endpoint.** Enrolment is an administrator action via the
CLI. A user-facing `POST /auth/mfa/enroll` has to be specified in `api-design.md` before it is
built (`CLAUDE.md` rule 1), and it brings its own questions — whether a session with one factor may
enrol a second, and what happens to sessions issued before enrolment.

**Membership has no history.** A revocation deletes the row and the audit log records it, so
`case_members` cannot answer "who was on this case in March" from the table itself. Recorded in
ADR-0017's Consequences as a deliberate limit — the append-only ledger already holds that — rather
than discovered later.

**The other ABAC attributes §6 lists remain open**: evidence classification vs. caller clearance,
`legal_authority_ref` presence, time-of-day context. This increment closes the case-scope attribute
only; `require_case_access` is the seam where the rest would go.

**There is no admin router at all.** `GET /api/v1/admin/audit-log` (api-design.md §10, PRD FR-9.3)
is unbuilt — the app includes health, auth and the module routers, and nothing else. It is an
export/read surface over a ledger that already exists and verifies, so it is additive; naming it
here so it is not mistaken for something this increment covered.

Next roadmap item is Wave 3.2 — the API idempotency store (ADR-0012, `platform.idempotency_keys`).

---

## 2026-09-29 — IC-038: Wave 3.1 committed; Wave 3.2 API idempotency (ADR-0012)

**Type:** Release of Wave 3.1, then the idempotency store `api-design.md` §2.9 has specified since
the API was designed. One migration, one new `platform` package, one generic seam on ADR-0005's
transaction boundary. No endpoint's contract changes.

### Wave 3.1 shipped

Access and API trust (IC-037) committed as `fb0dd50` (membership ABAC, ADR-0017) and `99eba54`
(MFA enforcement + session lifecycle, ADR-0010), then pushed.

### Wave 3.2: what was wrong

Twenty endpoints in `api-design.md` §4 are marked "Yes (key)", `Idempotency-Key` has been in §2.9
from the start, and **nothing implemented it**. A connector that timed out and retried
`POST /evidence` created a second evidence object — two chain-of-custody roots for one seizure, both
signed, both anchored, and no way for a verifier to say which was the real one. The header was
accepted and ignored, which is worse than rejecting it: a client doing exactly what the contract
asked got no protection and no error.

### ADR-0012 §2(b) says 422. The status is 409

The task for this wave specified `422` on a fingerprint mismatch, and so does the ADR. `api-design.md`
disagrees in three places: §2.4's error table lists `IDEMPOTENCY_KEY_CONFLICT → 409`, §2.9 spells out
"returns `409 IDEMPOTENCY_KEY_CONFLICT`", and `POST /evidence` lists "409 (idempotency)" among its
error codes. `CLAUDE.md` makes `api-design.md` authoritative for the REST contract, and
`shared/exceptions.py`'s own docstring requires every exception map 1:1 to a documented §2.4 code.

`409` is also right on the merits: `422` means the entity is semantically invalid, and here the entity
is a perfectly valid case or evidence object — what conflicts is the *reuse of the key*, which is a
state conflict. Implementing the ADR's `422` would have meant either contradicting the published
contract or editing three places in `api-design.md` to match an ADR that was wrong. The ADR is
corrected instead, with the reasoning recorded in its status block.

### Middleware could not satisfy §2(c), so it is a dependency plus a pre-commit hook

§2 offers "dependency/middleware" and §2(c) requires the response be persisted **in the same
transaction as the business write**. ASGI middleware runs outside the route's session entirely, so it
would have to open its own transaction — and could then commit a response record for a business write
that rolled back, or commit the write and lose the record. Both are the failure this ADR exists to
prevent, in opposite directions.

What is built is a **router-level dependency**. It shares the request-scoped session (FastAPI caches
`get_session`), and it can stop the handler by raising — which is what makes "no double effects" mean
*the business logic does not run twice*, rather than *its writes get deduplicated afterwards*. The
distinction is the whole point, and it is what the API tests assert: the service is wrapped in a
counting spy, and every replay test checks the counter, not just the body. A body-only test would pass
against an implementation that re-ran the handler and threw the second result away.

A replay reaches the client as an exception (`IdempotentReplay`) rendered by a registered handler,
because a dependency can only refuse to let the handler run. The refusal *is* the mechanism.

Recording the response needs a window no dependency can reach: after the handler returns, before the
commit. FastAPI runs dependency teardown *after* the response is produced — the same finding that made
ADR-0005 a route class instead of a `yield` dependency (IC-012). So `platform/db/transaction.py` gains
a generic `register_pre_commit` hook. It names nothing about idempotency; `platform.db` must not start
importing its siblings, and the hook is a seam rather than a dependency.

### The unique constraint is the concurrency control

§2(d) permits "serialize (row lock) **or** `409`". Serializing is strictly better — a client that
retried after a timeout wants the original answer, not a new error — and it needed no explicit
locking. Two simultaneous requests carrying one key both reach the claim `INSERT`; Postgres makes the
second wait on `uq_idempotency_claim` until the first transaction ends. First committed → the second's
insert fails and it replays the response now stored. First rolled back → the second's insert succeeds
and it proceeds.

That is why the claim lives in the **request's own transaction**, and the consequence is the design's
best property: a failed request's claim rolls back with it, so the client can fix the problem and
retry the same key immediately. A claim committed independently would outlive the failure it
accompanied and block every retry of that key for the full 24 hours — one transient error becoming a
day of them — and would need a reaper for abandoned claims. This design needs neither. `state =
'claimed'` is therefore never observable by another transaction, which is proven against Postgres
rather than argued.

### What is deliberately not cached

**Failures.** Most arrive as exceptions and roll back with their claim; a handler that *returns* a
4xx/5xx has its claim dropped explicitly. Caching a failure would block every retry of that key while
storing nothing worth replaying.

**Streaming responses.** No materialized body, and consuming the iterator to capture one would break
the response being sent. The claim is dropped — such an endpoint is not idempotency-cacheable, and
saying so by not caching beats storing an empty body and replaying it as the answer.

**Most headers.** An allowlist (`ETag`, `Location`, `Content-Type`), not a denylist: `Date`,
`Content-Length` and any request/correlation id describe *this* exchange, and replaying a stored copy
would hand a client another request's identifiers. A replay also carries `Idempotent-Replay: true`,
which §2.9 does not specify — without it a replay is indistinguishable from a fresh execution and
"did my retry take effect?" is unanswerable from the wire.

### The fingerprint covers more than §2.9's "body hash"

SHA-256 over `(method, path, principal, body)` with a `NUL` separator between fields, so
`("POST", "/a/b")` and `("POST/a", "/b")` cannot hash alike — the same domain-separation argument
ADR-0003 §2 makes for the ledger preimage. The method matters because `PUT` and `PATCH` on one path
with one body are different operations. The principal is redundant against the unique constraint
today and is included anyway, so that widening the lookup later (a service account acting for a user)
cannot silently let one caller replay another's response.

The body is hashed **verbatim**, not JCS-canonicalized. Canonicalizing would let a client resend
semantically-identical JSON with different whitespace and still replay — friendlier, and it means
parsing attacker-controlled input on the idempotency path before any handler has validated it, to buy
leniency in a case where the strict answer (conflict, retry fresh) is already safe. A body that will
not parse at all still gets a fingerprint.

### Tests

**41 new** — 13 pure fingerprint tests, 14 end-to-end over the real router stack, 14 against real
Postgres.

The Postgres file carries the two properties a fake cannot express: a genuine two-transaction race on
one key (with a barrier, so it is a race and not two sequential calls) proving exactly one claim
survives and the loser blocks rather than duplicating; and that a rolled-back claim frees its key
immediately.

One test failure was worth more than the test. `test_a_failed_request_leaves_no_claim` failed because
the fake session could not roll back a dict — and the first draft's comment papered over that by
asserting the guard's cleanup path instead. The fake now models pending-vs-committed rows and honours
`rollback()`, so the test asserts the real property. A fake that cannot fail the way production fails
is a fake that proves nothing.

### Gates

ruff (lint + format), `mypy --strict` (226 files), import-linter (2/2 kept — `platform` stays
domain-agnostic; the idempotency package imports nothing from any module). Full suite **1236 passed /
2 skipped**. Platform coverage **95.30%** against the 90% floor, with the new package at 91–100%
(`guard.py` 91%, everything else 100%). Migration round-trip green including the new revision.

### Carried forward

**The header is enforced when present, not yet mandatory.** §2.9 says it is *required* on the twenty
"Yes (key)" endpoints. Making it so would reject every request from the existing console and any
connector that does not send one — a client-breaking change belonging with a coordinated `apps/web`
and SDK update, not smuggled into the store that makes it possible. Recorded in ADR-0012's status.

**`DELETE` is excluded by design**, not omission: every keyed `DELETE` in §4 is marked *naturally*
idempotent, so storing a response for one would add a write to buy nothing.

Next roadmap item is Wave 4 (Phase 2) — CQRS graph read models (ADR-0013), multi-tenancy (ADR-0014),
observability, and DR/backup. Wave 3's remaining gaps are unchanged and still recorded in IC-037: SSO,
ADR-0010 A3's cookie transport, self-service MFA enrolment, and the admin router.

---

## 2026-09-29 — IC-039: Wave 3 gap closure — A3 cookie transport and the audit-log export

**Type:** Two gaps IC-037 and IC-038 recorded as outstanding, closed. One additive migration, one new
`platform` package, one new endpoint. No new ADR — both items were already specified and already
recorded as unbuilt.

### Gap 1: A3 was half-built, and the half that was missing was the transport

ADR-0010 A3 specifies two credentials: a short-lived access token in the `Authorization` header held
in JavaScript memory, and a long-lived refresh token in an `HttpOnly; Secure; SameSite=Strict` cookie
scoped to the refresh endpoint. Wave 3.1 built the **rotation** half against a single credential
carried in the request body — which meant the client had to hold the long-lived credential in
JavaScript to send it, the exact exposure the cookie exists to remove.

`platform.sessions` now carries two independently-generated 256-bit tokens per row, each as an
argon2id digest plus a lookup prefix. Independent generation is the point: a refresh token derivable
from an access token would make the script-exposed credential sufficient to mint new sessions.

**Two expiries, and this inverted an existing test.** `expires_at` is the access token's;
`refresh_expires_at` is the refresh token's. The access token must be able to expire *while the
session stays refreshable* — that is the entire purpose of the split — so `refresh` keys on the
refresh credential and checks `refresh_expires_at`. A path that resolved the access token would
refuse exactly the case it exists to serve, and would require a live access token in order to replace
one, which is circular.

`test_an_expired_session_cannot_be_refreshed` had asserted the opposite, correctly, when there was
one credential. It is now a pair: an expired **access** token must still refresh, and an expired
**refresh** token must not. A third test pins that the two credentials are not interchangeable —
separate repository methods matching separate digests, rather than one method accepting either, which
would make them a single credential with two names.

**One row, not a `refresh_tokens` table.** The credentials share one lifecycle: issued together,
revoked together, replaced together by rotation. A second table would model a one-to-one relationship
as a join and give `revoked_at` two places to disagree about whether a session is over.

**Pre-A3 sessions cannot be backfilled** and are not. The plaintext was never stored, so there is no
digest to derive; those sessions stay usable until their access token expires and are then simply not
refreshable. A placeholder digest would be a row claiming a credential exists when none does.

`cookies.py` owns every attribute, because A3's argument is a property of the whole set — three
handlers with three literal attribute lists would be three chances for the one that matters to drift.
`Secure` is keyed on the profile *name* rather than `is_production`, so `testing` gets it too: a test
profile is not a reason to hand out a cookie that could travel in clear.

Logout clears the cookie as well as revoking server-side. Revocation alone suffices for security, but
a cookie left in the jar means the browser keeps presenting a dead token and the resulting 401 is
indistinguishable, to the client, from a session that expired on its own. `/auth/logout` never
*receives* the cookie — its `Path` scopes it to the refresh endpoint — and clearing works anyway,
because `Set-Cookie` is applied from the response.

**A3's CSRF argument is now tested, not asserted.** It depends on no endpoint accepting the cookie as
authentication. `test_the_cookie_is_not_accepted_as_authentication` presents the cookie with no
`Authorization` header and requires a refusal.

### The access token is still 8 hours, and it is the one part of A3 still outstanding

A3 calls the access token "short". `session_ttl_seconds` stays at 8h because `apps/web` has no
refresh loop — `shared/api/client.ts` marks retry/refresh as "deliberately NOT here yet" — so
shortening it would log every analyst out mid-shift with no automatic recovery.

Stated plainly rather than left implicit: A3's benefit — "an XSS that steals the in-memory access
token still cannot mint new sessions past its short expiry" — is weakened while "short" means 8h.
What the transport buys *today* is that the long-lived credential is unreadable by script, so an XSS
cannot extend its reach past the stolen token's own window. Tightening the TTL is one line once the
console can refresh.

### A dead config field was squatting on a live name

`refresh_token_ttl_seconds` already existed — inside the block marked "RESERVED, NOT ACTIVE", left
from the JWT design ADR-0010 §2 explicitly rejected. Nothing read it. Now that the name has a real
meaning, two fields with one name is the drift trap Wave 2.4 removed from the CEM vocabulary
(IC-036), so the dead pair went: `refresh_token_ttl_seconds` is the live setting, and
`access_token_ttl_seconds` went with it because the access token's lifetime is `session_ttl_seconds`
and a second field claiming to be it would be read first and be wrong. A stale comment asserting the
refresh flow "is not built yet" was corrected in the same pass.

### Gap 2: the audit log had no export

`api-design.md` §4.1 has listed `GET /api/v1/admin/audit-log` (admin, compliance) and §10 has
detailed it since the API was designed; PRD FR-9.3 requires it. The app registered health, auth and
the module routers, and nothing else — so the one artifact an oversight body reads was reachable only
by someone with database credentials, which is precisely the person an audit is meant to check.

`platform/admin/` rather than a module: `platform.audit_log` is platform-owned — every module writes
through `record_audit_event`, none owns it — so an admin router inside `case_management` would be a
module reaching across a schema boundary for a table that is not its own.

**Ascending `(occurred_at, audit_id)`, which is a correctness requirement and not a preference.** The
export exists to be verified, and verifying `entry_hash` links needs the order the entries were
written in; newest-first would be friendlier to a UI and hand a reviewer a sequence whose links run
backwards. The composite key matters because `occurred_at` is not unique — two entries written in one
transaction can share a timestamp, and ordering by it alone would let a page boundary drop or
duplicate one. Keyset pagination, not OFFSET: the audit log only grows, and an OFFSET scan deep into
it gets slower the more history there is to export.

**The export carries the signature, not only the hashes.** Recomputing the chain proves the entries
are *self-consistent* — an insider who rewrote the whole chain would pass that check. Only the
signature proves they could not (ADR-0003 §1), so `signature` (base64), `sig_alg`, `key_id`,
`hash_algo` and `preimage_version` ship alongside `prev_entry_hash`/`entry_hash`. The agility fields
are nullable, and a verifier must report a null as *unprovable* rather than as a pass.

**Reading the audit log is deliberately not itself audited.** An entry per audit read would grow the
table on every page of an export and, because each entry chains to the last, interleave the reader's
own footprints into the chain being exported. The access is authorized and logged at request level;
§10 asks for neither more nor less.

The repository exposes **no write method**. §10: "There is no `DELETE` anywhere in this endpoint group
— the audit log has no API-level erasure path at all." A parametrized test asserts `DELETE`, `POST`,
`PATCH` and `PUT` all return 405, because the day someone adds a convenience mutation here is the day
the audit log stops being evidence.

### Tests

**48 new** — 18 session lifecycle (three of them new, replacing one whose premise A3 inverted), 11
cookie transport, 19 audit-log export.

The audit-log tests build a **real** chain through `record_audit_event` against Postgres rather than
fabricating hashes, and the pagination test walks the whole log two entries at a time and asserts the
reassembled sequence is every entry, once, in order, **with the links still joining across page
boundaries** — the way keyset pagination over a composite key actually breaks. The cookie tests read
the raw `Set-Cookie` header rather than httpx's parsed jar, because A3 is a statement about the
attributes and the jar flattens them.

### Gates

ruff (lint + format), `mypy --strict` (232 files), import-linter (2/2 kept). Full suite **1268 passed
/ 2 skipped**. Platform coverage **95.65%** against the 90% floor. Migration round-trip green
including the new revision.

### Carried forward

**SSO/OIDC/SAML** remains unbuilt — deferred by sequencing per ADR-0010 A1, explicitly not scoped out
of any profile. **Self-service MFA enrolment** remains CLI-only. **The access-token TTL** is the
remaining piece of A3, gated on the console's refresh loop. The rest of §4.1's admin group
(`/admin/users`, `/admin/roles`) is still unbuilt; provisioning is `sentinelai.cli.admin`.

Next roadmap item is Wave 4 (Phase 2) — CQRS graph read models (ADR-0013), multi-tenancy (ADR-0014),
observability, and DR/backup with independent integrity attestation.

---

## 2026-09-29 — IC-040: Wave 4.1 CQRS graph read models (ADR-0013); `get_case_graph` unblocked

**Type:** The first read/write split in the codebase. One new schema, one migration, two projectors,
one endpoint that had raised `NotImplementedError` since Phase 8. Resolves the roadmap's
benchmark-gated graph-store decision, with measurements.

### The deferral was structural, not a performance problem

`service.get_case_graph` had raised `NotImplementedError` for eight phases, and its docstring said
why: **no documented table maps a case to its entities.** Relationships reference evidence, cases
reference evidence, and `database-design.md` §5 forbids the cross-schema foreign key that would join
them. Every earlier attempt to schedule this read it as "graph queries are slow"; the query could not
be *written*.

`investigation.correlation_generated` already carries `case_id` beside `relationship_id`
(event-driven §25.8). The **event stream supplies the mapping the schema cannot** — and that is not a
loophole in §5, because nothing joins across a schema at query time: the join happened when the event
was published. That asymmetry is the whole reason CQRS applies here, and it is why this wave closes a
deferral that three earlier waves could not.

### §3's benchmark gate, resolved as "no graph datastore"

ADR-0013 §3 committed to adopting a graph store "only if measured CTE latency at target cardinality is
insufficient — a new datastore is a decision that requires evidence". Measured on the built projection
against PostgreSQL 16 (p50 of five samples):

| Entities | Edges | depth=1 | depth=3 | Returned at depth=3 |
|---|---|---|---|---|
| 1,000 | 3,000 | 15 ms | 75 ms | 993 nodes / 2,991 edges |
| 10,000 | 30,000 | 153 ms | 1,417 ms | 9,941 nodes / 29,942 edges |
| 50,000 | 200,000 | 1,793 ms | 13,996 ms | 49,975 nodes / 199,990 edges |

**The last column is the finding.** These are single dense components, so a 3-hop walk reaches
essentially everything: the 14 seconds is spent materializing and serializing 250,000 elements, not
traversing to find them. A graph database returns the same 250,000 elements just as slowly. So the
measurement does not say "CTEs are too slow" — it says a dense-component `depth=3` is the wrong query
to answer, and the limit is **response size**. `api-design.md` §6 already assumes a case subgraph is
bounded and offers no pagination, so the mitigation is a node cap on the response, not a storage
engine.

Decision: **stay on PostgreSQL.** The air-gapped profiles settle it independently of the numbers — a
second datastore is a second image to mirror into the enclave, a second backup path, a second set of
Vault credentials, and a second thing to patch on an offline update cycle, for a query shape that is
not the bottleneck. Apache AGE avoids the separate *service* but is still an extension that must be
present in the enclave image and version-matched. Recorded in ADR-0013 with the gate left open: a
*bounded* subgraph missing interactive latency would be new evidence.

### `investigation_read`, not `graph_read_models`

§1 asks for read/write separation, and a schema boundary is what makes that checkable rather than
aspirational — a query against `investigation_read` provably touches no transactional row, and the
projection can be truncated and rebuilt without a migration against live case data.

Named for its **owner** rather than its content, which is a deviation from the two names the task
suggested and the reason is `database-design.md`: §5 is schema-per-module and §11 orders migrations by
module, so a schema belonging to no module would have no Alembic chain and no defined position in the
ArgoCD PreSync sequence. §2's ownership table and a new §3.7 record it.

### Idempotency is doubled on purpose

Every projector performs the Inbox claim before any side effect (§17), **and** every write is an
`ON CONFLICT DO UPDATE` that converges. Either alone handles ordinary redelivery. Together they mean
the projection survives a replay that deliberately clears the inbox — which §Replay calls a normal
operation — and a future handler that forgets the claim. `is_seed` is folded with `OR` rather than
overwritten, so replay *order* cannot demote a seed and silently change what `depth` returns.

Investigation consumes its own published events here, which is deliberate rather than a loop: routing
the projection through the outbox is what makes it rebuildable by replay instead of a side effect
welded to the write path. A drop-and-replay test proves §2's rebuildability rather than asserting it.

### What bounds `depth`, stated rather than glossed

§6 defines `depth` as "hops from directly-evidenced entities". A relationship from
`correlation_generated` was generated for a case from evidence linked to it, so both endpoints are hop
zero. That is the only event that adds nodes, so **every projected node is a seed** and depth 1, 2 and
3 return the same subgraph — which is exactly what §6's default (`depth=1`) should return, but means
the traversal is inert today.

`depth` starts discriminating when the projection holds edges reaching outside the case's findings, and
feeding those needs an entity-level projection event §25.8 does not define. The recursive CTE is built
anyway, because it is §3's decision and because it makes `depth` correct on the day those edges arrive
rather than a migration away from it. Inventing the event would violate `CLAUDE.md` rule 1.

The task also named `entity.created` and `relationship.created` as projector inputs. **Neither exists**
— §163's catalog is explicit about the full set, and `entity.created` appears in the codebase only as
an audit *action* string. The projectors consume the two documented events that actually carry what a
graph projection needs.

### Three defects found

**The reachable set must not leave the database.** The first implementation resolved the walk to a
Python `set[UUID]` and fed it back as an `IN (...)` bind list. The benchmark killed it outright: asyncpg
caps a statement at 32,767 arguments, so a case with more reachable entities failed with
`InterfaceError` instead of returning a graph — and well under the cap it was shipping tens of thousands
of UUIDs out and back per request. The walk is now a subquery the planner joins against. This is the
bug the benchmark existed to find, and it would not have shown up in any functional test.

**Postgres allows one recursive term, not two.** Expanding "forward" and "backward" as separate
branches of the `UNION ALL` is invalid SQL (`InvalidRecursionError`). One term now matches an edge on
*either* endpoint and returns the other via `CASE`. Referencing the CTE through `.alias()` fails the
same way, by inlining its definition into the non-recursive term.

**A `Decimal` query parameter returned 500 instead of 400.** `min_confidence` is the codebase's first
non-JSON-native query parameter, and the `RequestValidationError` handler put `exc.errors()` — which
echoes the offending input, coerced to `Decimal` — straight into a `JSONResponse`. `json.dumps` raised
*inside the handler*, the unhandled-exception handler caught it, and an out-of-range value came back as
a server error with the offending field hidden. Fixed with `jsonable_encoder` and pinned by a new
`tests/unit/test_error_envelope.py`; it affects every route, not just this one.

### Two premises corrected against the code

`entities.confidence` and `relationships.confidence` are both **NOT NULL**. The projection columns were
drafted nullable "defensively", with a filter branch keeping unscored nodes and a test asserting an
analyst-registered entity survives a threshold — a state the write side cannot produce. Both the
nullability and the branch are gone: a projection column that admits a value its source cannot emit is
an invented state plus dead code to handle it.

`review_entity_status` publishes **nothing** ("audit only", and §25.8 defines no entity-disposition
event), so a projected node's `status` refreshes only when one of its relationships is re-projected.
Pinned by a test so it is a known property rather than a surprise.

### Tests

**48 new** — 24 projection/traversal against real Postgres, 18 HTTP contract, 6 error-envelope
regression.

The Postgres file covers what only a real database settles: that the recursive walk **terminates on a
cycle** (a test that would hang, not fail, if the hop bound were dropped), that filters are applied
*before* traversal so an excluded edge cannot act as a bridge to a node the caller should not reach,
and that a case's projection can be dropped and rebuilt to the same graph. The HTTP file proves the
endpoint refuses a non-member — `require_case_access`, the owner-or-member ABAC from Wave 3.1 — and
that the projection is not even queried for a refused caller.

### Gates

ruff (lint + format), `mypy --strict` (237 files), import-linter (2/2 kept — the read package imports
nothing outside `investigation`). Full suite **1316 passed / 2 skipped**. Platform coverage **95.65%**
against the 90% floor. Migration round-trip green including the new schema, which the downgrade drops.

### Carried forward

**A response node cap** — the benchmark says this, not a graph store, is the mitigation for a dense
subgraph. It is an `api-design.md` §6 change (a documented cap plus a truncation signal) and belongs
with that edit.

**The review-queue and statistics projections** (§1) are not built: the existing write-side queries
serve both and neither is a measured bottleneck.

**A scheduled rebuild job** is not built. `delete_case` plus replay is the mechanism and is tested;
scheduling it needs an operator trigger and a decision about what a rebuild does to a case being
actively read.

Next roadmap items in Wave 4: multi-tenancy (ADR-0014, gated on a product decision about deployment
profiles), observability (OTel over the `trace_id` already in the envelope), and DR/backup with
independent integrity attestation.

---

## 2026-09-29 — IC-041: Phase 2 begins — the OSINT connector pipeline (api-design.md §4.3, CEM §9)

**Type:** The first feature increment after eleven infrastructure ones. No new schema, no new table, no
new endpoint — the `osint` module's seven routes already existed and were already registered. What did
not exist was any of their behaviour.

### What was actually there

`osint` and `threat_intel` both looked complete from the outside: routers wired, migrations applied,
consumers registered, ETag headers declared. **Every service and repository method raised
`NotImplementedError`**, and neither module published a single event. The routers were signatures over
nothing — which is worse than absent, because the surface reads as working.

One domain, done end to end, rather than two half-built: **OSINT**. It is the self-contained pipeline
(register a source → capture a finding → normalize into the CEM), it has a fully-specified publish
contract in §4.3, and its events are documented in §25.3. `threat_intel`'s `ioc_matched` needs matching
IOCs against ingested evidence, which is a cross-module correlation problem rather than an intake one,
and is left for its own increment.

### The constraint that had to be resolved against the docs

The instruction was that the domain module "must not write directly to ingestion or investigation; it
must communicate strictly via outbox events". Strictly-via-outbox is not implementable against the
documented contract, and the conflict is worth recording rather than silently resolving:

`api-design.md` §4.3 specifies publish's outcome as a **`200` whose body carries `evidence_id`**, plus
an `ingestion.evidence_custody_events` genesis entry. An outbox hand-off cannot produce either — the
evidence does not exist when the response is written. §4.3 also says `evidence.ingested` is published
"**indirectly** ... by `ingestion` once the evidence row commits", which only makes sense if `osint`
called `ingestion` synchronously.

So publish calls `ingestion` through its `public.py`, which `CLAUDE.md` names as the sanctioned
cross-module path ("only through that module's `public.py`"); what is forbidden is importing another
module's `models.py`/`repository.py` or touching its tables, and neither happens. The stricter reading
of the constraint — no direct *table* access — holds. The import DAG already permits it
(`osint` sits above `ingestion`), and import-linter confirms it.

`ingestion.public` gained `EvidenceCreate` and `get_evidence_service`. Exporting the **provider** and
not just the class is the part that keeps the boundary honest: a sibling asks for a configured service
and never learns how one is built, so ingestion's storage and KMS wiring stay ingestion's.

### CEM §9's mapping profile does not exist, and was not invented

§9 step 2 specifies a "connector mapping profile — a versioned, declarative field-mapping definition,
**not** per-connector business logic embedded in the ingestion path". `database-design.md` §3.2 records
a `mapping_profile_version` on `connector_registry` and models **no table holding the profiles**. There
is nowhere to read a declarative mapping from.

Writing the per-connector logic §9 forbids was the wrong answer; so was inventing a table
(`CLAUDE.md` rule 1). Publish instead maps a **fixed envelope**: the connector states the CEM fields in
`raw_attributes`, and anything missing is a `422` naming exactly which. That keeps the mapping
declarative — the connector declares it — keeps §9's Validate step loud rather than silent (FR-1.3), and
leaves the profile store as a recorded gap. A finding whose fields are wrong stays captured and
re-publishable once a profile store exists.

Two things are deliberately **not** taken from the payload. `category` is fixed to `osint`, and `source`
is built from the registered `OsintSource` — a connector must not be able to attribute its output to a
different system or mislabel its own provenance.

### Where the pipeline's steps live

CEM §9 is Extract→Map→Enrich→Validate→Commit, and only the middle step is `osint`'s. Extract is the
connector's (`raw_attributes` holds the raw output, stored **unmodified** per §9 step 1 — validating its
CEM shape at capture would reject findings a future profile could handle, and the raw record is what an
examiner returns to when a mapping is later found wrong). Enrich, Validate and Commit are `ingestion`'s.
That split is the point: §13's rules and the custody genesis entry belong to the module that owns the
evidence table, and a second implementation here would give the platform two places to disagree about
whether an evidence object is admissible. A test asserts osint does not bypass it — an unregistered
`(schema_version, category, artifact_type)` triple is refused.

### Two bugs the tests found

**The mapping omitted `source.collector_id`.** `ingestion` requires both `system` and `collector_id` for
provenance; CEM §5's OSINT example shows `system` and `collection_method`, so the example's shape alone
was not sufficient. `collector_id` is now the registered source's own id, which ties every published
evidence object back to the exact feed configuration and survives the source being renamed.

**The findings cursor passed an ISO string into a `timestamptz` comparison.** Keyset pagination over
`(collected_at, finding_id)` needs the sort value parsed back to a `datetime`; the string produced a
Postgres type error rather than a wrong answer, which is the good failure mode — but only because the
row-value comparison is typed at all.

### Decisions recorded rather than left implicit

`osint.finding_captured` fires on **capture**, not publish. §25.3's trigger is "a connector or manual
entry creates a finding"; §4.3 also lists the event under publish's "Events Published". §25.3 is
authoritative for the event catalog and is what the code follows.

`register_source` publishes `osint.source_activated`, because a newly-registered source *is* newly
active — a consumer tracking live feeds would otherwise miss every source never toggled after creation.
`update_source` publishes only on an actual transition, so editing a reliability baseline does not
announce a toggle that did not happen.

The pre-publish finding status (`captured`) is an assumption: §3.3 requires a `status` column and §4.3
fixes only the post-publish value. It matches the event name so the two cannot drift into describing
different things.

`collected_at` is server-assigned. A connector does not get to state when the platform received its
finding — backdating a record into an already-anchored window is the failure ADR-0003's anchor watermark
exists to prevent, and a test pins it.

### Tests

**41 new** — 25 against real Postgres, 16 over HTTP.

The Postgres suite spans three schemas because the pipeline genuinely does: the finding in `osint`, the
evidence and custody entry in `ingestion`, the audit entry in `platform`. It **verifies the outbox
signature with the real `EventSigner`** rather than asserting the column is non-null — ADR-0007's claim
is that a consumer can prove provenance, and populated bytes that verify against nothing would satisfy
a weaker test. It also proves each module publishes only its own facts to its own outbox
(`osint.finding_captured` in `osint`, `evidence.ingested` in `ingestion`, and no `evidence.*` in
`osint`), which is the module boundary made observable.

The HTTP suite checks the contract a connector programs against: the `Idempotency-Key` replay, the
`409` on a reused key with a different payload, §4.3's per-endpoint RBAC, and the ETag guard. Both
idempotency assertions check a **call counter**, not just matching bodies — "a retried push does not
double-create" means the service was not re-entered.

### Gates

ruff (lint + format), `mypy --strict` (237 files), import-linter (2/2 kept — `osint → ingestion` is
within the documented DAG). Full suite **1357 passed / 2 skipped**. Platform coverage **95.65%** against the 90%
floor.

### Carried forward

**`threat_intel` is still a scaffold** — every service and repository method raises
`NotImplementedError`, as do `forensics` and `social_media`. The same is true of `osint`'s
`ConnectorStateRepository.get_for_source`, which exists for the polling loop that does not yet run: the
intake surface is built, the *automated collection* behind it is not.

**No connector mapping-profile store**, per above. Until it exists, every publisher must state the CEM
envelope itself, which works for manual entry and for a connector written against this API but not for
a declarative profile an operator edits.

**No OSINT polling schedule.** §25.3 describes osint as "driven by its own connector polling schedule";
nothing schedules one, so findings arrive only by push.

Next: `threat_intel`'s intake is the natural sibling increment, and the IOC-matching path that feeds
`threat_intel.ioc_matched` is the first genuinely cross-domain correlation work.

---

## 2026-09-29 — IC-042: threat_intel — IOC registration, profiling, feeds, and evidence matching

**Type:** Phase 2's second connector module, and the first genuinely cross-domain correlation path in
the platform. One migration (a unique index), one new pure module (`matching.py`), one event consumer
that does real work. `threat_intel` was a signature-only scaffold like `osint` before it.

### Two premises in the task did not survive the catalog

**`threat_intel.actor_profiled` does not exist.** §25.4 lists exactly two published events for this
module — `threat_intel.ioc_registered` and `threat_intel.ioc_matched` — and §163's compliance check
enumerates the whole platform catalog without it. Publishing an invented event would violate
`CLAUDE.md` rule 1, which requires a new event type be added to §25's catalog in the same change that
introduces it in code. Creating a threat actor therefore **publishes nothing** and is audited, which
is what api-design.md §4.4 asks for. `ioc_registered` — which the task did not mention — is published,
because the catalog says it is.

**Matching runs in one direction, not two.** The task asked to match "when new evidence is ingested or
IOCs are registered". §25.4 documents only the first: `threat_intel` *consumes* `evidence.ingested` and
scans the new evidence against active IOCs. Registering an IOC does not retro-scan history — no
document specifies it, it would turn one registration into an unbounded table scan, and the wall of
historical matches it produces is a different feature from "tell me when this indicator turns up".
Recorded as unbuilt rather than guessed at.

### A documented conflict, resolved toward the event catalog

§25.4 triggers `ioc_registered` on "New IOC created". api-design.md §4.4's table for the same endpoint
says "Events Published: **none at creation**". They cannot both hold. `CLAUDE.md` makes
`event-driven-architecture.md` the authority for everything async including "the complete per-module
published/consumed event catalog (Section 25)", so the event is published and the conflict is recorded
here rather than resolved silently in whichever direction was convenient.

### Matching is exact on a normalized token, and that is the whole design

The tempting implementation is `ioc.value in str(attributes)`. It is wrong in the direction that
matters: the IOC `evil.com` would "match" evidence mentioning `notevil.com` or `evil.com.br`, and an
analyst would be told a known-malicious domain appears in a case when it does not. **A false positive
here is not a cosmetic bug — it is an accusation in a legal record.**

So `matching.py` is pure and does two things. It **normalizes at registration**: hashes and domains
lower-case (hex and DNS are case-insensitive), IPs go through `ipaddress` so `2001:db8::0:1` and
`2001:0db8::1` collapse to one host, URLs fold scheme and host but keep the path verbatim because a
path *is* case-sensitive. And it **decomposes evidence into candidate tokens**, so comparison is
equality rather than containment. A URL contributes its host as well as itself, because evidence
recording `http://evil.com/x` should match a `domain` IOC for `evil.com` — otherwise the match would
depend on whether the connector happened to store a hostname or a URL.

Normalizing at registration rather than at match time is what makes the query cheap: the stored value
and the evidence tokens are put in the same shape once, so matching is **one indexed `IN` against a
token set**. The alternative — loop the IOC library and test each indicator against the evidence — gets
slower as the threat library grows, which is exactly backwards for the thing that runs on every ingest.

Hash length is validated against the declared type: a 32-character digest labelled `hash_sha256` is an
MD5 with the wrong label, and the label decides which evidence field it is ever compared against —
the same argument `IntegrityHash` makes in `shared/cem.py`. Tokenization is bounded in depth and
breadth, because `attributes` is connector-supplied and this runs on every ingest.

### Attributes come from `ingestion.public`, and the reader is a function

§181 keeps `attributes` off the event bus deliberately — they may be large or sensitive, and §21 wants
sensitive content off it entirely — and says "a consumer that needs it" fetches. So `ingestion.public`
gained `read_evidence_attributes(session, evidence_id)`.

**A function over a session, not an `EvidenceService` method**, and the reason is what the dispatcher
provides: a handler gets a session and a signed outbox, nothing more. No KMS, no object storage. A
service method would have meant constructing an `EvidenceService` with nulls for dependencies this
read never touches. It also takes no `actor`: `EvidenceService.get_evidence` takes a `CurrentUser` and
never consults it (authorization is the router's), so an actor-free reader is no bypass — it exists so
a consumer does not fabricate a principal, which would be a lie in every audit path it reached.

### Pair uniqueness is enforced twice, on purpose

§25.4 names `(ioc_id, matched_evidence_id)` as the handler's idempotency key: "never create a duplicate
match row for the same pair". The service checks before inserting **and** a new unique index enforces
it. The check keeps redelivery quiet; the constraint makes two concurrent scans impossible rather than
merely unlikely — two workers both pass the check, and only one insert can then succeed.

That belt-and-suspenders is worth the migration because a duplicate match is not cosmetic: each row
claims a known-malicious indicator appears in a specific piece of evidence, and each publishes an
`ioc_matched` event that `investigation` consumes — so a duplicate becomes a second correlation, a
second finding to review, and a second line in a report about one sighting.

A match is deliberately **not audited**. `platform.audit_log` records what principals did, and no
principal did this; the record of the observation is the match row plus the event, both attributable to
the platform rather than to whoever happened to upload the evidence. The event carries
`actor_type="system"` and no `actor_ref` for the same reason.

### A circular import, fixed by following the existing convention

`service.py` imported the event names from `events.py`; `events.py` needed the matcher from
`service.py`. `threat_intel` is the first module whose service both publishes an event *and* whose
consumer wanted that service, which is how the cycle appeared.

Every other module's `events.py` imports models and repository and **never** the service — handlers
work against the UoW directly (`case_management`, `notification`). Moving the matcher into `events.py`,
beside the handler that calls it, removes the cycle by following that convention rather than papering
over it with a deferred import. It also lands the matcher where it belongs: it is consumer-path logic,
and it needs no KMS, while every method left on `ThreatIntelService` is an audited user action that
does.

### Feed sync: the trigger is real, the transport is not

`sync_feed` validates the subscription, **refuses outright on a zero-egress profile**, audits the
request, and enqueues. The refusal is the security point: a feed sync is by definition an outbound call,
and `deployment-architecture.md` requires air-gapped and classified deployments to have "zero
configured or observed egress paths", so enqueuing a job that would attempt one — and might succeed
through a misconfigured proxy — is not an acceptable answer there. The worker re-checks the same
invariant, because a job can be enqueued on one profile and run after a redeploy onto another.

**The STIX/TAXII and vendor-API transport is not built**, and `jobs.py` says so rather than pretending:
it raises a named `FeedTransportNotConfigured`. The endpoint is `202`, so a failing job dead-letters
where an operator sees it instead of telling a caller the sync succeeded. Stamping `last_synced_at` and
returning would have recorded a synchronization that never happened, on the column an analyst reads to
decide whether their threat library is current — a feed that silently never updates while claiming it
did is worse than one that visibly fails.

### Tests

**97 new** — 47 pure (`matching.py`), 30 against real Postgres, 20 over HTTP.

The pure file is built around the cases a substring match gets wrong, because those are the ones that
produce a false accusation. The Postgres file proves the unique index holds when the service's check
loses a race, that a retired indicator costs nothing and matches nothing, that the same indicator in
two evidence items is two sightings rather than one, and that the `ioc_matched` event is **verified
under `EVENT_ROOT`** with the real signer rather than asserted non-null. The HTTP file asserts
idempotency on a **call counter** — "a retried submission does not register twice" means the service was
not re-entered — plus §4.4's per-endpoint RBAC, including that a feed integration running as `system`
may register IOCs but not create threat actors.

### Gates

ruff (lint + format), `mypy --strict` (239 files), import-linter (2/2 kept). Full suite
**1454 passed / 2 skipped**. Platform coverage **95.65%** against the 90% floor. Migration round-trip green
including the new unique index.

### Carried forward

**No feed transport**, per above — IOCs reach the platform through `POST /threat-intel/iocs` until a
connector increment builds one, and there is no scheduled feed poll.

**No retro-scan on IOC registration.** Registering an indicator does not search existing evidence; only
newly ingested evidence is scanned. Closing it needs a documented decision about scope and cost.

**`forensics` and `social_media` are still scaffolds** — every service and repository method raises
`NotImplementedError`, exactly as `osint` and `threat_intel` did.

**No IOC → evidence publication.** `iocs.evidence_id` exists and stays null: §3.3 allows an IOC to be
published into the CEM as its own evidence object, and no endpoint does that. Matching relates an IOC
to *other* evidence, which is a different relationship.

`threat_intel.ioc_matched` now flows to `investigation`, whose handler is still a deferred no-op — so
the matches are recorded and announced but do not yet become graph edges. That handler is the next
increment, and it is what would put non-seed edges into the Wave 4.1 projection and finally make
`depth` mean something.

---

## 2026-09-29 — IC-043: the closed intelligence loop — `ioc_matched` becomes a case-graph finding

**Type:** the increment IC-042 named as next. `investigation`'s `on_ioc_matched` was a deferred
no-op, so a threat-intel match was recorded and announced and then went nowhere. It now produces
graph findings, the Wave 4.1 projector consumes them, and `GET /api/v1/cases/{case_id}/graph`
returns them. One migration (a unique index), one new pure module (`payloads.py`), two handlers that
stopped being no-ops.

### A match is a node, not an edge to the evidence — and that is the model, not a shortcut

The task asked for an "`ioc_matched` edge linking the IOC entity to the evidence item". Three
documents say that shape does not exist, and the third is the one that decides it:

* **CEM §11** gives the graph one node type — `Entity` — and types its edges *between entities*.
  Evidence appears as a MENTIONS edge from "a lightweight `Evidence` reference node", not as a node
  the relationship layer can reach.
* **CEM §8's relationship vocabulary is closed** and holds no `ioc_matched` type. Adding one would
  be `CLAUDE.md` rule 1 ("never invent an endpoint, event, table, or field") and would also make the
  value unfilterable by any client written against the contract.
* **api-design.md §6's response body is `{ entities, relationships }`** and nothing else. An evidence
  node would have nowhere to be returned, so an edge pointing at one could never be resolved by a
  caller — which is precisely what §6's self-containment guarantee forbids.

So "indicator I is present in evidence E" lands as a **`digital_asset` entity** (CEM §7's type,
verbatim: "A file, domain, IP, URL, or indicator") **grounded by a MENTIONS row** — which is also
what satisfies CEM §13's rule that a non-analyst entity needs ≥1 MENTIONS edge to exist at all. It
is a node, because an edge needs two entities and a single match supplies one.

**Where the edge does come from.** If the same evidence item mentions another entity, the two
co-occur in it — and CEM §10 names "co-occurrence within the same evidence item" as a
relationship-inference target, which CEM §8 types `associated_with` ("Any ↔ Any", "generic, weighted
association where a more specific type doesn't apply"). That edge is grounded in the matched evidence
**honestly**: the evidence genuinely shows both. Today the usual case is two indicators in one
report — a C2 domain and a dropper hash — which is real intelligence an analyst wants. When an
extraction layer starts writing mentions, the same code relates an indicator to the people and
accounts named beside it, with no change here.

The confidence on that edge is the one number in this increment no document fixes, and it is flagged
as an assumption in the code: `0.500`. The co-occurrence is *certain*; what it implies about the two
being related is not, which is why §8 calls the type "weighted" and why the finding is `proposed` for
an analyst to dispose of (PRD FR-7.3). A real weight belongs to the correlation run's model.

**What is deliberately not built**: the threat-actor edge. `iocs.threat_actor_id` already records
the attribution, and the actor is a genuine entity one hop out — the first thing that would make
`depth` mean something. It is not built because the relationship would have **no honest supporting
evidence**: the case's evidence shows the indicator, not the attribution, whose provenance is the
feed. CEM §13 rejects a relationship without ≥1 supporting evidence, and inventing one would be a
provenance lie in a legal record. It unblocks when an IOC is published into the CEM as its own
evidence object (`iocs.evidence_id`, §3.3 — still null).

### Correcting IC-042: this does **not** make `depth` mean something

IC-042 closed by saying this handler "is what would put non-seed edges into the Wave 4.1 projection
and finally make `depth` mean something". That was wrong, and ADR-0013 now says so in the same
breath as recording what did change. Every node the projection holds is still a seed: a matched
indicator is mentioned by evidence linked to the case, and so is every entity it associates with, so
all of them are hop zero. `depth` 1, 2 and 3 still return the same subgraph. The thing that would
change it is the threat-actor edge above.

### Both orderings, because the common one is the awkward one

`on_ioc_matched` announces a finding per case the evidence is linked to **at match time**. The
common real sequence is the other way round: evidence is ingested and scanned within seconds, and an
analyst links it to a case minutes or days later. Without a second path every match that arrived
before the link would be invisible in that case **forever** — recorded on the write side, absent from
the read model, and with no event left to replay that would place it.

So `evidence.linked_to_case` stopped being a no-op too. §25.8's action for it is "mark evidence
eligible for this case's correlation runs" and §3.5 defines no eligibility table, but the useful half
of that sentence is expressible in a CQRS world: the case's graph should now show the entities this
evidence mentions and the relationships it supports. It **projects directly rather than publishing**,
because nothing was found — those rows already existed and were already announced when they were
created. A rebuild replays the same `case_management` event and reconstructs the same projection,
which is the property ADR-0013 §2 requires.

A match on evidence belonging to **no** case writes the entity and its MENTIONS row and publishes
nothing, because `case_id` is required in §25.8's `correlation_generated` payload — there is no
case-less form of the event, and the fact is about evidence rather than about a case. The link-time
projection is what places it later.

`evidence.unlinked_from_case` stays a no-op, and this one is a recorded gap rather than a decision:
retracting what an unlinked evidence item contributed needs per-evidence provenance in the
projection, and `investigation_read` holds none — a node can be grounded by several evidence items,
so "drop what this one brought" is not answerable from the rows. §25.8 also states the write-side
rule that an unlink does not invalidate already-`confirmed` relationships, so deleting projected rows
would contradict it. The projection is rebuildable, so the fix is a provenance column or a case
rebuild — a documented decision, not a handler detail.

### Two defects the loop surfaced, both in code shipped last increment

**`threat_intel.ioc_matched` was publishing an incomplete payload.** §25's payload schema for it
marks five fields required; the publisher sent three. `indicator_type` and `matched_at` were missing —
and `indicator_type` is exactly what the new consumer needs, so the loop would have been building
graph nodes from a payload the document already said should carry it. §25.4's catalog row lists only
three "key fields", which is what the implementation followed; the payload schema table is the
contract. Fixed, and the existing match test now asserts both fields.

**No handler-published event was setting `causation_id`.** §11 requires it ("the event/request that
directly caused this one") and §11's *worked example* is this exact chain —
`evidence.ingested → threat_intel.ioc_matched → investigation.correlation_generated`, "same
`correlation_id`, new `causation_id`". Without it the causal chain an auditor walks is broken at the
first hop. Fixed for `ioc_matched` and set on the new `correlation_generated` publishes. **Still
missing in `notification`**, whose publishes go through `NotificationService` methods that receive a
`correlation_id` string and never see the envelope — threading it through is a signature change
across several call sites and belongs in its own increment. Recorded here rather than half-done.

A service-level publish (one caused by an HTTP request, not by an event) correctly leaves it `NULL`:
§11 makes the field nullable precisely because there is no causing event id to point at.

### §25.8's catalog was missing two subscriptions Wave 4.1 added

ADR-0013 made `investigation` consume its own `correlation_generated` and `finding_reviewed`, and
`CLAUDE.md` requires a new subscription to be added to §25's catalog **in the same change**. It was
not. Both rows are recorded now, with the reason a module consuming its own events is deliberate, and
the module's consumed count corrected from 4 to 6.

### The cross-module reads, and why two of them are functions

`investigation` needs two things no event carries, and both are the §174 "thin event + reference"
fetch the document prescribes:

* **the indicator's value** — `ioc_matched` carries the IOC's *id* and type, not its value, so
  `threat_intel.public` gained `read_ioc`. A payload field would have been a MINOR bump (§7) to a
  contract the document pins at five required fields, and would put indicator values on the bus that
  §21 asks us to keep off it where a fetch will do.
* **which cases hold the evidence** — no evidence-bearing event carries a case, correctly, since the
  link is `case_management`'s fact and can change after the event. `case_management.public` gained
  `read_cases_for_evidence`, which returns the case **and its owner**, because
  `correlation_generated` carries `recipient_user_id` (§25.8) — a consumer that could not name one
  would publish a finding nobody is told about.

Both are **functions over a session, not service methods**, exactly like
`ingestion.public.read_evidence_attributes`: the dispatcher hands a handler a session and a signed
outbox, while `ThreatIntelService` and `CaseService` need a KMS (and storage) for audit and reports.
Neither takes an actor, and that is not an authorization hole — no principal is asking, and a consumer
that fabricated one would be lying to every audit path it reached. Nothing they return reaches a user
un-gated: the graph read re-checks ADR-0017's case access on every request, which one of the new tests
asserts directly.

`import-linter` keeps both honest: `investigation` sits above `case_management` and `threat_intel` in
§5's DAG, so the imports are legal, and they go through `public.py` — never a model, a repository or a
table.

### The writes happen on the consumer path, and a match is still not audited

Entities, MENTIONS rows and associations are created in `events.py`, not through
`InvestigationService`, for the reason IC-042 established for `threat_intel`: every method on that
service is an audited user action requiring a KMS the dispatcher does not provide. And a match is not
a user action — `platform.audit_log` records what principals did, and no principal did this. One of
the tests asserts exactly that: after a full loop the audit log holds the two IOC *registrations* and
nothing else. The findings carry `actor_type="system"` and no `actor_ref`.

### Idempotency, at both layers §12 requires

`(ioc_id, matched_evidence_id)` is §25.8's business key, and it lands here as
`(entity_id, evidence_id)` — the indicator entity is resolved from the IOC's value, so the pair is the
same fact. Checked before inserting **and** enforced by a new unique index, `uq_entity_mention_pair`.

The index is the half the check cannot provide: two dispatchers can both pass the check, and only one
insert then succeeds. It is worth a migration because a duplicate MENTIONS row is not bookkeeping — it
would double-count the evidence grounding a finding under CEM §13. It is also simply *true* of the
table as §3.5 models it: there is no offset, span or count that could distinguish two rows for one
pair, so "this evidence mentions this entity" is set membership.

The fan-out is **capped at 25** co-mentioned entities per match, logged when it bites. `attributes`
are connector-supplied and a future extraction layer will write many mentions per evidence item;
without a bound one crowded report could turn a single match into a review queue nobody can work
through.

### Tests

**33 new** — 16 unit, 12 end-to-end against real Postgres, 5 on the projector.

The end-to-end file has no fakes in the middle of the chain: a signed `evidence.ingested` row goes
into `ingestion`'s outbox, the **real dispatcher** in `VERIFY_STRICT` claims and verifies it,
`threat_intel` matches and publishes, the dispatcher verifies *that*, `investigation` writes findings
and publishes, the projector writes `investigation_read`, and the assertion is an HTTP `GET` on the
real route. Because the dispatcher runs in strict mode, every assertion that a handler ran is also an
assertion that the event it ran on **verified** under `EVENT_ROOT` — a forged one would be
`dead_letter` with its handler never invoked.

It covers the two idempotency layers separately, which matters because they fail differently: a
redelivered `event_id` (inbox), and a **replay that clears the inbox** and re-delivers, where the
business key is the only thing standing between an operator's re-scan and a second copy of every
indicator in the graph. Plus the unique index holding when a duplicate is inserted behind the check,
the link-after-match ordering, one indicator in two evidence items staying one node, and a withdrawn
IOC projecting nothing rather than dead-lettering.

The unit file uses in-memory repositories to reach the decisions the loop cannot isolate: malformed
payloads, an unparseable confidence, no case links, several case links, and the fan-out cap (asserted
through the real constant, so raising it is a deliberate edit in one place).

### Gates

ruff (lint + format), `mypy --strict` (241 source files), import-linter (2/2 kept). Full suite
**1487 passed / 2 skipped**. Migration round-trip green including the new index.

**Coverage — a pre-existing CI failure this increment found and did not cause.** The gate CI runs is
`pytest tests/unit --cov=sentinelai.platform --cov-fail-under=90`, and it reports **84.65%**. Verified
pre-existing by stashing this work: byte-identical, 3675 statements and 564 missed either way. The
**96%** figure reported in earlier increments is the same measurement over the *whole* suite, which is
the number that reflects the intent ("≥90% on platform") — the missing statements are almost entirely
`platform/admin/*` (0% under unit-only), `auth/repository.py` (39%) and `idempotency/guard.py` (39%),
all of which are covered, by integration tests that CI runs in a different job with no coverage flag.
So the code is tested and the *command* measures the wrong suites. Left as found, and flagged: the fix
is either to attach the floor to the job that has a database, or to write DB-mocked unit tests for
code whose whole job is SQL — and choosing between those is not a decision to make silently inside an
unrelated increment.

### Carried forward

**`depth` still returns the same subgraph at 1, 2 and 3**, per the correction above. The threat-actor
edge is what changes that, and it is blocked on publishing an IOC into the CEM.

**Unlinking evidence does not retract projected graph rows** — recorded above, needs a provenance
column or a rebuild.

**`notification` still publishes without `causation_id`**, breaking §11's chain at its last hop.

**No notification is raised for a matched indicator.** `notification`'s `correlation_generated`
handler requires a `relationship_id` and ignores the entity variant, so the case owner is told about
the association but not about the indicator on its own. §25.9's idempotency key for that handler is
relationship-based, so extending it needs a documented key for the entity variant rather than a
guess.

**`forensics` and `social_media` are still scaffolds**, and there is still no feed transport and no
retro-scan on IOC registration (IC-042).

---

## 2026-09-29 — IC-044: Wave 4.3 — OpenTelemetry tracing across the event bus, and the CI coverage gate

**Type:** the observability half of Wave 4.3, plus the gate fix IC-043 surfaced. One new ADR
(**ADR-0018**, the write-up `engineering-roadmap.md`'s register has carried as pending for the
monitoring stack), one new platform module, five new runtime dependencies, no schema change — the
`trace_id` column has been in the envelope since §9 was written and every publisher wrote `NULL`
into it.

### The CI coverage gate: the command was wrong, not the tests

IC-043 reported the finding; this closes it. CI's coverage job ran `pytest tests/unit
--cov=sentinelai.platform --cov-fail-under=90` and got **84.65%**, and had been failing for several
increments while the increments themselves reported ~96%. Both numbers were real: 96% is the same
flag measured over the whole suite.

The gap is not untested code. Over half of `platform` is code whose entire job is SQL —
`platform/admin/*` (0% under unit-only), `auth/repository.py` (39%), `idempotency/guard.py` (39%) —
and it is thoroughly tested, by the integration tests CI runs in a *different* job with no coverage
flag. A unit-only floor on a database-heavy platform layer measures the wrong suites and cannot be
satisfied except by mocking a database, which would be tests that assert the mock.

So the floor moved to the job that has a database, over **unit and integration together**:
**95.37%**, and `--cov-fail-under=90` now passes for the reason it was supposed to. The unit job
stays, without the floor, because it fails in about a minute rather than after four containers come
up. Unit tests run twice in CI; that costs a minute and is the cheaper half of the trade.
`make test-coverage` runs exactly the CI command, and `COVERAGE_FLOOR` still lives in one place.

**This is a gate being pointed at the right thing, not a gate being lowered** — the threshold is
untouched at 90 and the measured figure went *up*, because the suites that exercise the code are now
included. Recorded explicitly because "the gate was wrong" is the most abusable sentence in
engineering.

### What crosses the event bus is a `traceparent`, not a trace id

§9 gives every event a nullable `trace_id` and §11 defines it as "W3C Trace Context, generated at the
entrypoint ... possibly spanning process/network boundaries". The column now carries a **full
`traceparent`** — `version-traceid-spanid-flags` — and the difference is the whole mechanism.

A consumer handed only the 32-hex trace id could label its span with the right trace but could not be
a **child** of anything in it. The result renders in Tempo as a pile of siblings with no shape, which
answers "did this happen?" but not "what made this take nine seconds?" — the only question worth
adding tracing for. §11's own wording anticipates this: the field "changes at every process/network
boundary rather than staying constant like `correlation_id` does", which is true of a `traceparent`
and false of a bare trace id.

Captured in `OutboxWriter.publish` and nowhere else, because the span that belongs on an event is the
one open when the business transaction ran. By the time the dispatcher relays the row, that span is
closed and its context is gone; there is no later point at which this is recoverable.

### `None`, never a zeroed placeholder — because the field is signed

`trace_id` has always been inside ADR-0007's signed field set. So `current_traceparent()` returns
`None` when nothing is being traced rather than a synthetic all-zero traceparent: a signature is an
attestation, and attesting to an execution path that never existed would make an evidentiary record
say something false about how the evidence moved. The cost of getting this wrong is not a confusing
dashboard.

A property fell out of that which was not a design goal and is worth having: **tampering with a
stored `trace_id` is detected as forgery.** Editing the column breaks the signature, so the row
dead-letters with a `critical` log line instead of quietly claiming the event came from a different
request. The execution path recorded on an event is now as tamper-evident as its payload. The other
face of the same coin: `trace_id` cannot be back-filled or corrected in place on a signed row, which
is the right trade for a field inside an attestation. Both are tested.

### The bug the tests found: `context=None` means "inherit", not "no parent"

`context_from_traceparent` returns `None` for anything unusable — absent, malformed, or well-formed
with an invalid span context — because `extract` does **not** raise on garbage: it returns the
*current* context unchanged. A handler that passed that through would parent the consumer span onto
whatever the dispatcher happened to be inside, inventing a causal link. In a platform whose purpose
is proving provenance, a fabricated causal edge is the worst available failure.

The guard was written first and was **defeated by the SDK's own semantics**: passing `context=None`
to `start_as_current_span` does not mean "no parent", it means "use the current context" — exactly
the ambient inheritance the guard existed to prevent. The integration test caught it only because it
drives the dispatcher from *inside* an unrelated span; with no ambient context, the right and wrong
implementations are indistinguishable. Fixed with a named `parent_context()` that returns an
explicitly empty `Context`, and the trap is documented where the knowledge lives rather than in a
commit message.

`extracted or Context()` would have been the natural spelling and is also wrong: an OTel `Context` is
a dict, so one carrying only a span is still falsy, and a perfectly good parent would be discarded.
Both halves are pinned by tests.

### Export is opt-in, and that is the air-gapped invariant

`deployment-architecture.md` rule 6 requires air-gapped and classified deployments to have "zero
configured or observed egress paths — verify, don't assume". A default OTLP endpoint — any default —
would be a configured path out of the enclave that nobody chose. `OTEL_EXPORTER_OTLP_ENDPOINT` is
therefore empty by default with no fallback.

With no exporter the SDK still runs: spans are created, context still propagates through the outbox,
and they are dropped at the processor. That is deliberate. It means the propagation path is exercised
on every deployment and across the whole test suite, rather than being a code path that only executes
where nobody is watching — the arrangement that lets tracing rot silently between releases.

**Air-gapped deployments can still be traced**, and a test asserts it, so a later "harden the
air-gapped profile" change cannot quietly take observability away from the deployments that can least
afford to debug blind. A collector inside the enclave is east-west traffic, not egress; what the rule
forbids is an endpoint the *platform* chose. The invariant that is actually checkable is enforced
instead — console export is refused in production-grade profiles, where a span per request on stdout
would bury the structured events Promtail ships to Loki.

OTLP over **HTTP/protobuf, not gRPC**: the gRPC exporter pulls in `grpcio`, a native extension whose
platform-specific wheels turn an offline mirror into a build toolchain. Tempo accepts both. Same
reasoning `asn1crypto` already carries in `pyproject.toml`.

### One span per handler, and `trace_id` on the log line

A span per *handler*, not per event: two handlers on one event succeed and fail independently, and a
single span over both would attribute one's failure to the other. Failures set the span status and
record the exception, because a span that ended `OK` while its log line said otherwise sends an
operator to the wrong place.

`trace_id` is bound into the structlog context in both processes — the HTTP middleware for a request,
the dispatcher for a handler — which is what makes Part 20's promise (Grafana joining a Loki line to
a Tempo trace) true. `platform/logging.py`'s docstring had claimed the middleware did this since it
was written; it did not. Now it does, and the docstring says under what condition.

It is deliberately **not** echoed to clients as a response header, unlike `X-Request-Id` and
`X-Correlation-Id`: §11 is explicit that `trace_id` "has no business meaning", and handing a caller a
handle on internal execution topology describes the system to anyone who asks for nothing in return.

### A test fixture that broke 111 unrelated tests

Worth recording because the failure mode is so misleading. The tracing tests install a provider and
restore the previous one, and the obvious spelling is wrong:
`previous = trace.get_tracer_provider()` returns a **proxy** when none is set, and that proxy
resolves every call by reading the module global — so restoring it *into* that global makes it
delegate to itself. Every later test that built a FastAPI app died with `RecursionError`, in a
different file, and only when a tracing test shared the session with them. A single-file run was
green.

The fix is to read the module global directly (unset is `None`), and it lives in
`tests/fixtures/tracing.py` beside the second piece of the same knowledge:
`set_tracer_provider` is guarded by a module-level `Once`, so clearing the provider without resetting
that guard leaves the next call logging "Overriding of current TracerProvider is not allowed" and
doing nothing — a test would then assert against whatever provider a previous test installed.

### Tests

**41 new** — 29 unit, 12 against real Postgres.

The Postgres file fakes nothing between the two ends: a real `OutboxWriter` signs and inserts a row
inside an active span, the real `EventDispatcher` claims it in **strict** signature mode, and the
handler's span is inspected through a real in-memory exporter. Because verification is strict, every
assertion that the handler ran is also an assertion that the event verified with a populated
`trace_id` — a trace that broke event authentication would be a trade nobody agreed to. It also
proves the chain survives a second hop: an event published *by* a handler carries the same trace with
the handler's span as its parent, which is how `evidence.ingested → ioc_matched →
correlation_generated` renders as one path.

The unit file is built around absence — no active span, malformed input, a profile that forbids the
exporter someone configured — and around two traps: the `context=None` semantics above, and
`instrument_sqlalchemy` needing `engine.sync_engine` (passing the async wrapper attaches to nothing
and fails **silently**, so it looks instrumented in review).

### Gates

ruff (lint + format), `mypy --strict` (242 source files), import-linter (2/2 kept). Unit +
integration **1524 passed / 1 skipped**; migrations, architecture, contract and performance green.
Platform coverage **95.37%** against the 90% floor, measured by the command CI now runs.
`platform/tracing.py` is at **100%**.

### Carried forward

**No spans for arq jobs.** There is no `opentelemetry-instrumentation-arq`, and hand-rolling one is
its own piece of work. The worker still produces the dispatcher's consumer spans and the SQLAlchemy
client spans beneath them, so a scheduled job's execution is visible in logs and metrics but not as a
trace.

**Wave 4.3's alerting half is not built.** Part 20's alert-routing table names the signals;
`infra/` carries no Alertmanager configuration, and no OTel collector manifest ships with this change
either — the endpoint is a setting with nothing on the other end of it yet.

**No OTel metrics or logs pipeline**, deliberately (ADR-0018 §7): Prometheus and structlog already
serve both, and Part 20's architecture is three pipelines into one Grafana, not one pipeline.

**Tail sampling is not configured** — `OTEL_TRACES_SAMPLE_RATIO` is a head-sampling knob at 1.0.
Keeping the slow and failed traces specifically is collector-side configuration, and belongs with the
`infra/` work above.

**`notification` still publishes without `causation_id`** (IC-043), so §11's causal chain still breaks
at its last hop even though the *trace* now spans it.
