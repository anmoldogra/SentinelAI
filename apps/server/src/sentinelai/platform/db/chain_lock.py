"""Serialization for hash-chain appends — ADR-0003, ADR-0004.

Appending to a hash chain is a read-modify-write: read the current head, build an entry that names
it, insert. Nothing in that sequence is atomic on its own, so two concurrent writers can read the
same head and both insert, **forking the chain** into two branches that each verify perfectly. On a
custody ledger that is not a tidy-up item: a fork means two contradictory histories of the same
evidence, both internally consistent, with no way to tell from the data which is the real one.

The window was always open. It became materially wider when ADR-0003 §1 signing landed, because a
KMS round-trip now sits between the head read and the insert — sub-millisecond became a network
RTT against Vault.

**Two mechanisms, doing different jobs.**

1. **Unique constraints make a fork impossible** (migrations ``202609080003_platform_chain`` and
   ``202609080004_ingestion_chain``). ``audit_log(prev_entry_hash)`` unique means each entry hash
   has at most one successor, which is the structural definition of a chain rather than a tree.
   The custody ledger gets the same on ``(evidence_id, prev_event_hash)`` plus
   ``(evidence_id, sequence_number)``. This is the correctness guarantee, and it holds even if the
   head query returns the wrong row, if the advisory lock below is somehow skipped, or if a writer
   bypasses the service layer entirely: the second inserter gets an ``IntegrityError`` and its
   transaction dies. Fail closed.

2. **This advisory lock stops writers wasting work racing for that constraint.** Without it,
   concurrent appends each pay a full KMS signature before one of them loses on insert. With it,
   they queue. The lock is the liveness optimisation; the constraint is the correctness guarantee.
   Never reason about this the other way round — an advisory lock is cooperative and protects
   nothing against a writer that does not take it.

``pg_advisory_xact_lock`` is used rather than a session-level lock or ``SELECT ... FOR UPDATE``:

* It is released automatically at commit **or rollback**, which matches ADR-0005's
  entrypoint-owns-the-transaction model. A session-level lock leaked by an exception would
  wedge the chain until the connection was recycled.
* There is no row to lock for the audit ledger — its chain is global, and its head row is
  precisely what a concurrent writer is about to add. ``FOR UPDATE`` on "the newest row" cannot
  serialize an insert of a *newer* one.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Namespaces, so two different chains can never collide onto one lock key and serialize each
# other for no reason. These strings are part of the runtime contract: changing one lets writers
# that disagree about it append concurrently.
AUDIT_CHAIN = "platform.audit_log"
CUSTODY_CHAIN = "ingestion.evidence_custody_events"

_SIGNED_64_MAX = 2**63


def advisory_lock_key(namespace: str, discriminator: str | None = None) -> int:
    """Derive the ``bigint`` key for one chain's advisory lock.

    Hashed rather than hand-assigned so a new chain cannot silently reuse another's number, and
    ``blake2b`` rather than ``hash()`` because Python's is salted per process — two workers would
    compute different keys for the same chain and cheerfully append at the same time.

    Postgres takes a *signed* 64-bit key, so the digest is folded into that range.
    """
    seed = namespace if discriminator is None else f"{namespace}:{discriminator}"
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) - _SIGNED_64_MAX


async def lock_chain(
    session: AsyncSession, namespace: str, discriminator: str | None = None
) -> None:
    """Serialize appends to one chain for the remainder of the caller's transaction.

    Blocks until the lock is free. There is deliberately no ``try``-variant here: a caller that
    could not take the lock has no safe alternative to waiting, and skipping it to stay responsive
    would trade a queue for a fork.

    ``discriminator`` narrows the lock to a sub-chain — custody passes the ``evidence_id``, so
    appends to different evidence items do not queue behind each other. The audit ledger passes
    nothing, because it genuinely is one global chain, and that is a throughput ceiling worth
    knowing about rather than working around.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": advisory_lock_key(namespace, discriminator)},
    )


__all__ = ["AUDIT_CHAIN", "CUSTODY_CHAIN", "advisory_lock_key", "lock_chain"]
