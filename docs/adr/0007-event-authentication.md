# 7. Event Authentication (Signed Outbox Envelopes)

## Status

**Accepted — Built** in modernization Wave 2.3. ADR-0009's dependency is satisfied:
`KeyPurpose.EVENT_ROOT` is the signing identity.

| Decision | State |
|---|---|
| §1 Sign every outbox row (Ed25519, canonical envelope) | **Built** — `platform/events/signing.py`; `OutboxWriter.publish` signs inside the publisher's transaction |
| §2 Consumers verify before processing; reject rather than process | **Built** — `EventDispatcher._verify` runs before any handler and before the inbox claim; a rejected event is quarantined as `dead_letter` and never retried |
| §3 Writer restriction (INSERT only by the owning module's role) | **Not built** — ADR-0004's role model provides the mechanism; the per-schema outbox grant is not yet narrowed to the owning module. Defence in depth, independent of §1/§2 |
| §4 Bind the `event_id` inside the signed envelope | **Built** — `event_id` is in the signed message, so a forged or replayed id is detectable independent of inbox dedup |
| §5 Transport-independent | **Built by construction** — the signature is over a canonical JSON message with no transport in it; on the Redpanda migration it travels in a header and `verify` is unchanged |

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

## Implementation note (2026-09-28, Wave 2.3)

### Three verification outcomes, not two

§2 says to reject an event "whose signature is absent or invalid". Implemented as three outcomes
because absent and invalid are not the same fact and must not be collapsed:

* **verified** — delivered.
* **absent** — delivered only under `permissive`. A row written before this wave genuinely has no
  signature and cannot gain one: signing it now would attest to bytes nobody witnessed at
  publication. A deployment carrying real data needs a window in which such rows still flow.
* **invalid** — never delivered, in either mode.

Tolerating a present-but-invalid signature under `permissive` would hand an attacker a downgrade
path: corrupt the envelope and a forgery is treated as merely unsigned. So permissive tolerates only
absence. This is the same distinction ADR-0003 §6 draws between *not provable* and *forged*.

`EVENTS_SIGNATURE_MODE` (`strict` | `permissive`) is the roadmap's rollback lever for this wave.
Strict is the default because this platform has no pre-signing production data to carry. Permissive
logs a warning per unsigned event and increments
`sentinelai_event_signature_failures_total{reason="missing_tolerated"}`, so a migration window is
never silent.

### Rejected events are quarantined, never retried

A bad signature will not become good. Retrying would only delay the alarm while consuming the retry
budget, so a rejected event goes straight to `dead_letter` with the reason recorded in `last_error`
— on the row, so an operator investigating does not have to correlate against a log line. The log
line is `CRITICAL`, because under this ADR's own threat model a forged event is an insider with
write access fabricating a domain fact, not a delivery hiccup.

### Verification happens before the inbox claim

Deliberately, and §2's wording ("before processing") is why. The inbox is a deduplication mechanism,
not an authentication one — it keys on `(event_id, handler_name)`, and a forger mints a fresh
`event_id`. Claiming the inbox first would also record a forged event as seen.

### Signing fails closed, and where the signer comes from

`OutboxWriter.publish` signs before it inserts, inside the publisher's transaction. A KMS outage
therefore aborts the whole business write rather than committing a fact the bus cannot authenticate
— the same trade ADR-0003 §1 makes for the evidentiary ledgers.

The signer reaches a publisher through its module's UoW (`get_<module>_uow` injects the process KMS;
job wrappers pass `ctx["kms"]`). For events published *by a handler* — `notification.dispatched` is
the live case — the dispatcher attaches the signer to the handler's UoW, because the module-supplied
`uow_factory` contract takes only a session and changing it in eight modules to thread a KMS would
be a wide change for a composition concern.

A publisher with no signer writes `NULL`, which is the honest representation of having no signing
identity wired. That is not silent: under strict verification the event is refused at consume time,
so the gap surfaces loudly rather than becoming an unauthenticated fact on the bus.

### The schema is part of the signed message

Not in §1's wording, and added because without it a row lifted verbatim from one module's
`outbox_events` into another's would carry a genuine signature over genuine content and verify. The
schema makes cross-module transplantation detectable even when every other field is identical — the
same role the ledger name plays in ADR-0003 §1's signed message.

### §3 remains open, and that is a real residual

Writer restriction is not implemented. Until it is, forging an event requires the signing key *or*
nothing at all in permissive mode — with §1/§2 built and strict mode on, it requires the key, which
is the substantive guarantee. §3 would additionally require the owning module's database role,
making cross-module forgery need two independent compromises. It belongs with an ADR-0004 grant
narrowing, not here.

## Consequences

- Forged/injected events are rejected; publisher non-repudiation.
- Per-publish signing cost (batchable; keys cached in-process from KMS with rotation).
- Schema: add `signature`, `key_id`, `sig_alg` to the generic outbox table shape; verification
  in the dispatcher. Consumers gain a verify step before the inbox claim.
