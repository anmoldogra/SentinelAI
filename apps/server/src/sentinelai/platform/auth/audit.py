"""Audit logging — the single, unbypassable write path (security §22, database-design §10).

``platform.audit_log`` is a hash-chained, insert-only ledger: each row's ``entry_hash`` is
computed over the previous row's hash plus **every** persisted field of this event, so any
deletion or edit breaks the chain and is detectable (PRD SR-4). Every module records audit events
through this function, never by inserting into the table directly.

**Wave 1.2 (ADR-0003 §2) made that preimage complete.** It previously covered only
``{prev, action, target_id, details}`` — omitting ``occurred_at``, ``actor_user_id``,
``actor_role``, ``module``, ``target_type``, ``ip_address`` and ``user_agent``. Those omissions
were not incidental: they are precisely the attribution fields, so the record of *who* did a
thing, *in what role*, and *from where* could be rewritten without breaking a single hash in the
chain. An audit ledger that cannot bind attribution is not an audit ledger.

Encoding is RFC 8785 JCS (``platform.crypto.canonical``), which matters most for ``details``:
it is a ``JSONB`` column, so Postgres discards key order on write and returns keys in its own
order. Hashing ``json.dumps`` output meant a row re-read from the database could not reproduce
its own hash. See ``platform.crypto.ledger`` for the shared hashing primitive and for why
``hash_algo``/``preimage_version`` are inside the hash rather than beside it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.crypto.ledger import (
    LEDGER_AUDIT,
    LEDGER_HASH_ALGO,
    LEDGER_PREIMAGE_VERSION,
    LedgerSigner,
    compute_entry_hash,
    ledger_timestamp,
    ledger_uuid,
)
from sentinelai.platform.db.chain_lock import AUDIT_CHAIN, lock_chain

_GENESIS_HASH = "0" * 64


async def _get_last_entry_hash(session: AsyncSession) -> str:
    """Take the chain lock, then return the current head hash (genesis for an empty ledger).

    The lock is acquired **before** the read and held to the end of the transaction, so the head
    this returns is still the head when the caller inserts. Without it, two writers read the same
    head, both sign, and one loses on the unique index — correct, but only after paying for a KMS
    round-trip it has to throw away. See ``platform.db.chain_lock``: the constraint is what makes
    a fork impossible, this is what stops writers racing for it.

    Ordering by ``occurred_at`` is a heuristic — clocks are not guaranteed monotonic — but it
    cannot produce a fork. If it ever named a non-head row, the entry built on it would collide
    with that row's real successor on ``uq_audit_log_prev_entry_hash`` and the transaction would
    fail rather than branch.
    """
    await lock_chain(session, AUDIT_CHAIN)
    result = await session.execute(
        select(AuditLog.entry_hash).order_by(AuditLog.occurred_at.desc()).limit(1)
    )
    return result.scalar_one_or_none() or _GENESIS_HASH


def _compute_hash(
    *,
    prev_hash: str,
    audit_id: UUID,
    occurred_at: datetime,
    actor_user_id: UUID | None,
    actor_role: str,
    action: str,
    module: str,
    target_type: str | None,
    target_id: UUID | None,
    ip_address: str | None,
    user_agent: str | None,
    details: dict[str, Any] | None,
) -> str:
    """Chain this entry onto the previous one over its complete persisted field set.

    Every column of ``platform.audit_log`` is covered except the four that cannot be: ``entry_hash``
    (this function's own output), and ``signature``/``key_id``/``sig_alg``/``anchor_ref``, which are
    written after the hash exists — the signature covers the hash, and the anchor covers a Merkle
    root built from many hashes. ``tests/unit/test_ledger_preimage.py`` enforces that list against
    the live table definition, so a column added later cannot quietly escape the preimage.

    Keyword-only: this takes eleven values of which four are ``str | None``, and a positional call
    that transposed ``target_type`` and ``ip_address`` would still typecheck and still hash — just
    to a different, wrong digest.
    """
    return compute_entry_hash(
        {
            "prev": prev_hash,
            "audit_id": str(audit_id),
            "occurred_at": ledger_timestamp(occurred_at),
            "actor_user_id": ledger_uuid(actor_user_id),
            "actor_role": actor_role,
            "action": action,
            "module": module,
            "target_type": target_type,
            "target_id": ledger_uuid(target_id),
            "ip_address": ip_address,
            "user_agent": user_agent,
            "details": details,
        }
    )


async def record_audit_event(
    session: AsyncSession,
    *,
    kms: KeyManagementService,
    actor_user_id: UUID | None,
    actor_role: str,
    action: str,
    module: str,
    target_type: str | None = None,
    target_id: UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one authenticated audit entry, on the caller's open transaction.

    ``kms`` is required, with no default. That is the point: an unsigned audit entry must be
    impossible to write, and a default would make one reachable by omission. Every call site
    supplies a KMS or fails to compile.

    **This performs a KMS operation inside the caller's transaction, and fails closed.** If the
    signing backend is unreachable the write raises and the whole transaction aborts — the audited
    action does not happen. That is the correct trade for a legal record (an unsigned entry is a
    hole no later process can fill, because the bytes that should have been signed are gone), but
    it is a real coupling: with a remote KMS, signing latency is added to every audited write and a
    KMS outage stops them. ADR-0003 anticipates this and points at batched Merkle signing (Wave
    1.3) as the mitigation.
    """
    prev_hash = await _get_last_entry_hash(session)
    # Both are generated here rather than left to a column default, because both are part of the
    # preimage: the row's own identity is bound to its hash, so an entry's contents cannot be
    # transplanted onto a new `audit_id` and still verify.
    audit_id = uuid4()
    occurred_at = datetime.now(UTC)
    entry_hash = _compute_hash(
        prev_hash=prev_hash,
        audit_id=audit_id,
        occurred_at=occurred_at,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=action,
        module=module,
        target_type=target_type,
        target_id=target_id,
        ip_address=ip_address,
        user_agent=user_agent,
        details=details,
    )
    # Signed before the insert, so a signing failure means no row rather than an unsigned one.
    signature = await LedgerSigner(kms).sign(
        ledger=LEDGER_AUDIT,
        # `audit_log` has no sequence column — its ordering is the chain itself. `entry_hash`
        # already covers `audit_id` and every other persisted field, so identity is bound anyway.
        sequence=None,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    await session.execute(
        insert(AuditLog).values(
            audit_id=audit_id,
            occurred_at=occurred_at,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action=action,
            module=module,
            target_type=target_type,
            target_id=target_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
            prev_entry_hash=prev_hash,
            entry_hash=entry_hash,
            hash_algo=LEDGER_HASH_ALGO,
            preimage_version=LEDGER_PREIMAGE_VERSION,
            signature=signature.envelope,
            sig_alg=signature.sig_alg,
            key_id=signature.key_id,
        )
    )
