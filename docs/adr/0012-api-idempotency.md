# 12. API Idempotency

## Status

**Accepted — Built** in modernization Wave 3.2. Depends on ADR-0005 (transaction boundary), which
has been in place since Wave 2.1. Resolves the missing implementation of `api-design.md` §2.9.

| Decision | State |
|---|---|
| §1 `platform.idempotency_keys`, unique on `(principal_id, key, path)` | **Built** — `202609290008_platform_idem`; the constraint is also the concurrency control |
| §2(a) Same key, same fingerprint → replay the stored response | **Built** — replayed byte-for-byte, and the handler does not run |
| §2(b) Same key, different fingerprint → reject | **Built as `409 IDEMPOTENCY_KEY_CONFLICT`, not `422`** — see the note; `api-design.md` §2.4 and §2.9 both say 409 |
| §2(c) Claim, process, persist the response in the business transaction | **Built** — a router dependency claims, a pre-commit hook on ADR-0005's boundary records |
| §2(d) Concurrent duplicate → serialize (row lock) or `409` | **Built as serialization** — the unique index makes the second claim wait; no in-flight state machine, no abandoned-claim cleanup |
| §3 TTL + cleanup job, keys scoped per principal | **Built** — 24h per §2.9 (`idempotency_ttl_seconds`), swept nightly by `purge_expired_idempotency_keys` |

## Implementation note (2026-09-29, Wave 3.2)

### §2(b)'s `422` was wrong; the status is `409`

This ADR says `422` for a same-key-different-fingerprint retry. `api-design.md` — the authoritative
REST contract — has said otherwise since the API was designed: §2.4's error table lists
`IDEMPOTENCY_KEY_CONFLICT → 409`, §2.9 spells out "returns `409 IDEMPOTENCY_KEY_CONFLICT`", and
`POST /evidence` lists "409 (idempotency)" among its error codes.

`409` is also the correct code on the merits. `422` means the entity is well-formed but semantically
invalid; here the entity is fine — a perfectly valid case or evidence object — and what conflicts is
the *reuse of the key*. That is a state conflict, which is what `409` is for. Implementing the ADR's
`422` would have meant either contradicting the published contract or editing three places in
`api-design.md` to match an ADR that was wrong, so the ADR is corrected instead.

### A dependency plus a pre-commit hook, not middleware

§2 offers "dependency/middleware", and middleware cannot satisfy §2(c). ASGI middleware runs outside
the route's session entirely, so it would have to open its own transaction — and could then commit a
response record for a business write that rolled back, or commit the write and lose the record.
Either produces the exact failure this ADR exists to prevent, in one direction or the other.

So the decision is a **router-level dependency**: it shares the request-scoped session (FastAPI
caches `get_session`), and it can stop the handler by raising, which is what makes "no double
effects" mean *the business logic does not run twice* rather than *its writes get deduplicated
afterwards*.

Recording the response needs a window that no dependency can reach — after the handler returns,
before the commit. FastAPI runs dependency teardown *after* the response is produced, the same
reason ADR-0005 is implemented as a route class rather than a `yield` dependency. Wave 3.2 therefore
adds a generic `register_pre_commit` hook to that route class. `platform.db` knows nothing about
idempotency; the hook is a seam, not a dependency.

A replay arrives at the client through an exception (`IdempotentReplay`) rendered by a registered
handler, because a dependency can only refuse — it cannot return a response. The refusal *is* the
mechanism.

### The unique constraint is the concurrency control

§2(d) allows "serialize (row lock) or `409`", and serializing is strictly better: a client that
retried after a timeout wants the original answer, not a new error. No explicit locking was needed.
Two simultaneous requests carrying one key both reach the claim `INSERT`; Postgres makes the second
wait on the `uq_idempotency_claim` index entry until the first transaction ends. If the first
committed, the second's insert fails and it replays the response that is now stored. If the first
rolled back, the second's insert succeeds and it proceeds.

This is why the claim lives in the **request's own transaction** rather than a separate committed
one, and the consequence is worth stating plainly: a failed request's claim is rolled back with it,
so the client can fix the problem and retry the same key immediately. A claim committed
independently would outlive the failure it accompanied and block every retry of that key for the
full 24 hours — turning one transient error into a day of them — and would require a reaper for
abandoned claims that this design does not need. `state = 'claimed'` is consequently never observed
by another transaction, which is proven rather than assumed
(`test_a_claim_is_invisible_to_another_transaction_until_it_commits`).

### What is deliberately not cached

**Failure responses.** Most arrive as exceptions and are rolled back with their claim. A handler
that *returns* a 4xx/5xx instead has its claim dropped explicitly, because caching a failure would
block every retry of that key while storing nothing worth replaying.

**Streaming responses.** They have no materialized body, and consuming the iterator to capture one
would break the response being sent. The claim is dropped: such an endpoint is simply not
idempotency-cacheable, and not caching says so honestly, where storing an empty body would replay
nothing as though it were the answer.

**Most response headers.** Only `ETag`, `Location` and `Content-Type` are stored — an allowlist, not
a denylist. `Date`, `Content-Length` and any request/correlation id describe *this* exchange, and
replaying a stored copy would hand a client another request's identifiers. A replay additionally
carries `Idempotent-Replay: true`, which §2.9 does not specify; without it a replay is
indistinguishable from a fresh execution and "did my retry take effect?" is unanswerable from the
wire.

### The fingerprint covers more than the body

§2.9 describes storing a "request body hash". The stored fingerprint is a SHA-256 over
`(method, path, principal, body)` with a `NUL` separator between fields — the same domain-separation
argument ADR-0003 §2 makes for the ledger preimage, so that `("POST", "/a/b")` and `("POST/a", "/b")`
cannot hash alike. The method matters because `PUT` and `PATCH` on one path with one body are
different operations; the principal is redundant against the unique constraint today and is included
so that widening the lookup later cannot silently let one caller replay another's response.

The body is hashed **verbatim**, not JCS-canonicalized. Canonicalizing would let a client resend
semantically-identical JSON with different whitespace and still replay, which sounds friendlier but
means parsing attacker-controlled input on the idempotency path before any handler has validated it
— to buy leniency in a case where the strict answer (conflict, so the client retries fresh) is
already safe.

### Scope: enforced when present, not yet mandatory

§2.9 says the header is *required* on the twenty endpoints marked "Yes (key)". What is built honours
a key on any `POST`/`PUT`/`PATCH` to a module router and is a no-op without one. Making the header
**mandatory** on those endpoints would reject every request from the existing console and connectors
that do not send one yet — a client-breaking change that belongs with a coordinated `apps/web` and
SDK update, not smuggled into the store that makes it possible. Recorded here as not built.

`DELETE` is excluded: every keyed `DELETE` in §4 is marked *naturally* idempotent, so storing a
response for it would add a write to buy nothing.

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
