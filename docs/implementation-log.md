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
