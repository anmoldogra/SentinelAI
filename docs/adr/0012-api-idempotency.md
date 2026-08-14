# 12. API Idempotency

## Status

Proposed. Depends on ADR-0005 (transaction boundary). Resolves the missing implementation of
`api-design.md` §2.9.

## Context

`api-design.md` §2.9 specifies an `Idempotency-Key` header and several endpoints are marked
"Yes (key)" (e.g. `POST /evidence`, `POST /cases`), but there is **no idempotency store**. A
client retry after a network partition/timeout therefore double-creates resources — including
evidence, which is unacceptable.

## Decision

1. **`platform.idempotency_keys` table:** `(key, principal_id, method, path,
   request_fingerprint, response_status, response_body, created_at, expires_at, state)`, unique
   on `(principal_id, key, path)`.
2. **Dependency/middleware** on mutating routes: given an `Idempotency-Key`, (a) if a completed
   record with the **same** fingerprint exists → replay the stored response; (b) same key,
   **different** fingerprint → `422`; (c) no record → claim the key, process, and persist the
   response **in the same transaction as the business write** (ADR-0005); (d) concurrent
   duplicate in-flight → serialize (row lock) or `409`.
3. **TTL + cleanup job**; keys scoped per authenticated principal.

## Consequences

- Safe client retries with no double effects — essential for evidence ingest over unreliable
  agency networks.
- A new table + middleware; tight coupling to the entrypoint UoW so the response is stored
  atomically with the effect it describes.
- Adds one write per idempotent request; negligible at the documented request rates.
