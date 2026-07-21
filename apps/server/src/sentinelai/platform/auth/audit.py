"""Audit logging — the single, unbypassable write path (security §22, database-design §10).

``platform.audit_log`` is a hash-chained, insert-only ledger: each row's
``entry_hash`` is computed over the previous row's hash plus this event's fields,
so any deletion or edit breaks the chain and is detectable (PRD SR-4). Every module
records audit events through this function, never by inserting into the table directly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import AuditLog

_GENESIS_HASH = "0" * 64


async def _get_last_entry_hash(session: AsyncSession) -> str:
    """Return the most recent chain hash, or the genesis hash for an empty ledger."""
    result = await session.execute(
        select(AuditLog.entry_hash).order_by(AuditLog.occurred_at.desc()).limit(1)
    )
    return result.scalar_one_or_none() or _GENESIS_HASH


def _compute_hash(
    prev_hash: str,
    action: str,
    target_id: UUID | None,
    details: dict[str, Any] | None,
) -> str:
    """Deterministically chain this entry onto the previous one (SHA-256)."""
    preimage = json.dumps(
        {
            "prev": prev_hash,
            "action": action,
            "target_id": str(target_id) if target_id is not None else None,
            "details": details,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(preimage.encode()).hexdigest()


async def record_audit_event(
    session: AsyncSession,
    *,
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
    """Append one tamper-evident audit entry, on the caller's open transaction."""
    prev_hash = await _get_last_entry_hash(session)
    entry_hash = _compute_hash(prev_hash, action, target_id, details)
    await session.execute(
        insert(AuditLog).values(
            audit_id=uuid4(),
            occurred_at=datetime.now(UTC),
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
        )
    )
