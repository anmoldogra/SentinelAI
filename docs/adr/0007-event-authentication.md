# 7. Event Authentication (Signed Outbox Envelopes)

## Status

Proposed. Depends on ADR-0009 (Key Management).

## Context

Events are unauthenticated: `event_id` is publisher-minted `uuid4` (verified `outbox.py:95`)
and rows carry no signature. Under the platform's threat model, an insider who can `INSERT`
into a schema's `outbox_events` can **forge domain facts** (`evidence.superseded`,
`case.status_changed`) that every consumer processes as authentic. The inbox only deduplicates
by `(event_id, handler_name)`, which a freshly-minted id defeats.

## Decision

1. **Sign every outbox row.** Each event carries a detached **Ed25519 signature** (ADR-0009
   key) over its canonical envelope, produced by the **owning module's service identity**.
2. **Consumers verify before processing.** The dispatcher/handler rejects an event whose
   signature is absent or invalid; rejected events are quarantined, not processed.
3. **Writer restriction (defense in depth).** Ties to ADR-0004's role model: `outbox_events`
   in schema X is INSERT-able only by X's service role — cross-module forgery then requires
   *both* a signing key and a DB role.
4. **Bind the id.** `event_id` remains uuid4 but is inside the signed envelope, so a replayed
   or forged id is detectable independent of inbox dedup.
5. **Transport-independent.** On the Redpanda migration the signature travels in the message
   header; verification logic is unchanged.

## Consequences

- Forged/injected events are rejected; publisher non-repudiation.
- Per-publish signing cost (batchable; keys cached in-process from KMS with rotation).
- Schema: add `signature`, `key_id`, `sig_alg` to the generic outbox table shape; verification
  in the dispatcher. Consumers gain a verify step before the inbox claim.
