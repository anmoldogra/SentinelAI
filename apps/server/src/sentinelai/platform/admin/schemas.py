"""Admin response schemas — api-design.md §10.

Never return an ORM model from a route (guide Part 6), and for this endpoint in particular the
mapping is the contract: an audit entry is the record an oversight body reads, and the fields it
exposes are the ones `database-design.md` §10 says a reviewer needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditLogEntryRead(BaseModel):
    """One audit-log entry, including the hash-chain links.

    ``prev_entry_hash`` and ``entry_hash`` are here because api-design.md §10 requires them: they
    let an external reviewer recompute the chain and satisfy themselves it has not been tampered
    with, without trusting this API's word for it. Omitting them would make the export a report
    about the audit log rather than evidence from it.

    The signature columns are exposed too (``signature`` base64-encoded, plus ``sig_alg``/``key_id``
    and ``hash_algo``/``preimage_version``). A reviewer who can recompute the chain but not verify a
    signature can only prove the entries are *self-consistent* — an insider who rewrote the whole
    chain would pass that check, and ADR-0003 §1's whole point is that they cannot forge the
    signatures. The agility fields say which algorithm and key to verify under; they are nullable
    because entries written before Wave 1.1 have none, and a verifier must report those as
    *unprovable* rather than treating a null as a pass.
    """

    model_config = ConfigDict(from_attributes=True)

    audit_id: UUID
    occurred_at: datetime
    actor_user_id: UUID | None
    actor_role: str
    action: str
    module: str
    target_type: str | None
    target_id: UUID | None
    ip_address: str | None
    user_agent: str | None
    details: dict[str, Any] | None
    prev_entry_hash: str
    entry_hash: str
    hash_algo: str | None
    sig_alg: str | None
    key_id: str | None
    preimage_version: int | None
    signature: str | None


__all__ = ["AuditLogEntryRead"]
