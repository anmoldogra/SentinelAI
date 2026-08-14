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
