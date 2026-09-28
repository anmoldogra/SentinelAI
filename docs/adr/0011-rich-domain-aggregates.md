# 11. Rich Domain Aggregates for the Evidentiary Core

## Status

**Accepted — Built for the evidentiary core** in modernization Wave 2.4. The ADR-0005 dependency is
satisfied: the entrypoint has owned the transaction boundary since Wave 2.1.

| Decision | State |
|---|---|
| §1 `Case` owns `open→closed→archived` | **Built** (Wave 1, IC-011) — `case_management/models.py`; the service orchestrates and cannot reach an illegal state |
| §1 `Evidence` owns custody, legal hold and supersession | **Built** — `assert_can_record_custody`, `apply_custody_event`, `assert_supersedable`, `integrity`; the service delegates instead of re-checking. Method names differ from the sketch above — see the note |
| §1 `Finding`/`Relationship` owns `proposed→confirmed\|rejected` and the ≥1-supporting-evidence rule | **Built** — a shared `_Reviewable` mixin on `Entity` and `Relationship`. The evidence rule sits at **creation**, where CEM §13 actually puts it — see the note |
| §2 Value objects for the CEM concepts | **Built** — `shared/cem.py`: `IntegrityHash`, `ConfidenceScore`, `EvidenceCategory`, `ArtifactType`, `LegalAuthorityRef`, `CustodyEventType` |
| §3 Domain events vs integration events explicit in code | **Not built** — publication is still a direct service-to-outbox call. Deliberately deferred; see the note |
| §4 Belt-and-suspenders over ADR-0004's database-enforced append-only | **Holds** — unchanged by this wave; both layers still enforce independently |

## Context

The domain model is anemic: ORM rows carry no behavior, and every invariant — evidence
write-once, custody monotonicity + hash linkage, case/finding state machines, the CEM §13
"≥1 supporting evidence" rule — is enforced only inside service methods. A single code path or
raw SQL statement that bypasses the service silently violates a **legal** invariant. Tactical
DDD (aggregates, value objects, domain events) is absent despite the DDD framing.

## Decision

1. **True aggregates own their invariants:**
   - `Evidence` — owns its custody ledger and integrity/supersession rules; illegal mutation is
     impossible because the aggregate exposes only `record_custody(...)`, `supersede(...)`,
     `apply_legal_hold(...)` and refuses invalid transitions.
   - `Case` — owns the `open→closed→archived` machine.
   - `Finding`/`Relationship` — owns `proposed→confirmed|rejected` and the ≥1-supporting-evidence
     invariant.
2. **Value objects** for CEM concepts with validation at construction: `IntegrityHash`,
   `ConfidenceScore` (0–1), `EvidenceCategory`/`ArtifactType`, `LegalAuthorityRef`,
   `CustodyEventType`.
3. **Domain events vs integration events made explicit in code** (event-driven §4): aggregates
   raise domain events; the application layer maps the curated subset onto the outbox.
4. **Belt-and-suspenders:** aggregate-enforced invariants sit on top of ADR-0004's
   database-enforced append-only — an invariant is protected at both layers.

## Implementation note (2026-09-28, Wave 2.4)

### The ≥1-supporting-evidence rule is a creation invariant, not a review gate

The Context above calls it "the CEM §13 '≥1 supporting evidence' rule", which reads as a rule about
findings in general. The CEM is more specific, and the difference decides where the check belongs:
§1.6 says "**No Entity or Relationship may exist** without at least one supporting evidence
reference", and §13's validation table says *Reject*. It is an **existence** invariant.

It was first built here as a guard on confirmation, and a unit test caught it. Checking at
confirmation is wrong twice over: too late, because the unsupported row already exists, and perverse,
because it would refuse to let an analyst **reject** an unsupported finding — which is exactly what
should happen to one. The guard is now `Relationship.assert_supporting_evidence(count)`, called at
creation, where the service already enforced it inline.

It is a classmethod, because it is asked before the instance exists, and it takes a count rather than
reading a relationship, because the supporting rows are written in the same transaction as the
finding: there is nothing to query yet.

CEM §13 also grants entities an explicit exception — an analyst-pre-registered entity needs no
`MENTIONS` edge — so `Entity` creation deliberately does not call it. Only `Relationship` does.

### `Evidence` has no `apply_legal_hold`, and should not

§1 above sketches `record_custody(...)`, `supersede(...)` and `apply_legal_hold(...)`, all of which
presume the aggregate *sets* state. ADR-0004's append-only trigger rejects an `UPDATE` on the evidence
table outright, and ADR-0015 makes `status` and `legal_hold` **derived** from the custody ledger, so
the built shape is the same rule expressed for an append-only store:

* `assert_can_record_custody(event_type)` — a guard, so a caller can be refused *before* the expensive
  work of hashing and signing a ledger entry.
* `apply_custody_event(event_type)` — folds the event's effect in and returns the new hold state, or
  `None` for the events that say nothing about holds. The service writes it with
  `set_committed_value`, keeping the in-memory instance consistent with the ledger without dirtying it.
* `assert_supersedable(already_superseded=...)` — deliberately ignores the `status` column, because
  the genesis value never changes; trusting it would let a second supersession through on any row
  whose derived overlay had not been applied.

### Aggregates are the ORM classes, not a parallel domain model

`Case` established this in IC-011 and `Evidence`/`Entity`/`Relationship` follow it. A declarative
instance is an ordinary Python object until it meets a session, so every invariant here is exercised
in `tests/unit/test_aggregates.py` with no database, no fixtures and no engine — which is the property
the Consequences below actually ask for ("aggregate unit tests without a DB").

So the "aggregate↔ORM mapping layer" the Consequences anticipate was **not** built. A separate
pure-domain object plus a mapper would buy stricter purity at the cost of a translation step on every
read and write, and a second place for the shape of an evidence record to drift. That trade is not
worth making for invariants already expressible where the data is.

### What the aggregates deliberately do not own

**Queries.** `assert_supersedable` takes a flag and `assert_supporting_evidence` takes a count. An
aggregate that loaded either would need a session, which would make it untestable without a database
and would hide a query inside an invariant check. The *query* stays in the service; the *decision* is
the aggregate's.

**ETag / optimistic concurrency.** Checked in the service, before the aggregate is asked to mutate
anything. It is an HTTP-level concern with no domain meaning — the aggregate's job is that the
transition is legal, not that the caller held a fresh representation.

### The value objects live in `shared`, not in `ingestion`

The canonical evidence model is a cross-domain contract, and two modules already need the vocabulary:
`ingestion` for evidence, `investigation` for entity and relationship confidence. Putting it in
`ingestion` would make `investigation` depend on another module to name a number between zero and one.
`shared` is the lowest layer in the import DAG, so every module may use it and none is coupled to
another; `platform` has no reason to import it and does not.

Three choices inside them worth recording:

* `IntegrityHash` checks that the **digest length matches the algorithm**. A 64-character value
  labelled SHA-512 is not a truncated SHA-512 — it is a SHA-256 with the wrong label, and a verifier
  trusts the label.
* `CustodyEventType.legal_hold_state` is **three-valued** (`True`/`False`/`None`), because collapsing
  "this event says nothing about holds" into `False` would make every `accessed` event silently
  release a legal hold.
* `EvidenceCategory` and `ArtifactType` validate **shape, not membership**: the vocabulary is extended
  by registering an attribute schema, so a closed enum would make adding a category a code change and
  would reject data a correctly-registered connector is entitled to send. `CustodyEventType` **is**
  closed, because every value has specific meaning to the custody rules — an unrecognised one is not
  extensibility, it is an event nothing knows how to reason about sitting in a legal record.

`ConfidenceScore` refuses a `float` outright rather than coercing one. A score is compared against
thresholds and persisted as `NUMERIC`; accepting binary floating point would make `0.7` mean a
different number in the domain than in the database.

### §3 is not built, and that is a scope decision

Aggregates do not raise domain events for the application layer to map onto the outbox; publication
remains a direct `outbox.publish(...)` call from each service. That change touches every publisher in
the codebase and is independently valuable, so it is better as its own increment than as a rushed half
of this one. Nothing in §1 or §2 depends on it, and the guarantee that matters — that an event is
authentic and ordered — is ADR-0006's and ADR-0007's, both built.

## Consequences

- Invariants become structurally hard to violate; the domain is far more testable (aggregate
  unit tests without a DB) and readable.
- Adds an aggregate↔ORM mapping layer and a migration of the three implemented modules to the
  aggregate pattern (moderate, mechanical).
- Slightly more indirection; justified precisely because these invariants are legal guarantees,
  not conveniences.
