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
